"""Tool decoration.

``quantity_tool`` wraps a function so that arguments are validated and normalised before
the body runs, results are validated before they leave, and both are written to the
provenance ledger. The body itself receives ``Q`` values and so is written in terms of
physical quantities rather than bare floats.
"""

from __future__ import annotations

import functools
import inspect
from datetime import datetime
from typing import Any, Callable

from .errors import GuardViolation, UnconvertedCarryOver
from .provenance import WouldBlock, carry_over_message
from .provenance import active_session
from .quantity import Q
from .spec import Spec


class GuardedTool:
    """A callable tool with declared physical types."""

    #: ``strict`` rejects a violation, ``warn`` records it and continues on the raw value,
    #: ``off`` performs no checks. ``warn`` exists so a team can measure what enforcement
    #: would block before switching it on.
    ENFORCEMENT = ("strict", "warn", "off")

    def __init__(
        self,
        fn: Callable,
        params: dict[str, Any],
        returns: Any = None,
        name: str | None = None,
        description: str | None = None,
        enforcement: str = "strict",
    ):
        if enforcement not in self.ENFORCEMENT:
            raise ValueError(f"enforcement must be one of {self.ENFORCEMENT}")
        self.fn = fn
        self.name = name or fn.__name__
        self.description = (description or inspect.getdoc(fn) or "").strip()
        self.enforcement = enforcement
        self.params = {k: Spec.coerce_spec(v) for k, v in params.items()}
        self.returns = self._read_returns(returns)
        self.signature = inspect.signature(fn)
        unknown = set(self.params) - set(self.signature.parameters)
        if unknown:
            raise ValueError(f"{self.name} declares specs for unknown parameters: {sorted(unknown)}")
        functools.update_wrapper(self, fn)

    @staticmethod
    def _read_returns(returns: Any) -> Any:
        if returns is None:
            return None
        if isinstance(returns, dict) and not {"unit", "datum", "crs", "tz", "quality",
                                              "description", "require_explicit_unit"} & set(returns):
            return {k: Spec.coerce_spec(v) for k, v in returns.items()}
        return Spec.coerce_spec(returns)

    # Invocation ------------------------------------------------------------------------

    def __call__(self, *args, **kwargs) -> Any:
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()

        session = active_session()
        if session is not None:
            session.calls += 1

        for name, spec in self.params.items():
            if name in bound.arguments:
                raw = bound.arguments[name]
                value = self._coerce(spec, raw, name, session)
                bound.arguments[name] = value
                if session is not None and isinstance(value, Q):
                    session.record(self.name, "input", name, value)

        result = self.fn(*bound.args, **bound.kwargs)
        result = self._validate_result(result)

        if session is not None:
            for name, value in self._iter_quantities(result):
                session.record(self.name, "output", name, value)
        return result

    def _coerce(self, spec: Spec, raw: Any, name: str, session) -> Any:
        """Validate one argument under the tool's enforcement mode."""
        if self.enforcement == "off":
            return self._lenient(spec, raw, name)
        try:
            value = spec.coerce(raw, field=name)
            if session is not None and isinstance(value, Q):
                self._reject_carry_over(session, raw, value, name)
        except GuardViolation as violation:
            if self.enforcement == "strict":
                raise
            if session is not None:
                session.violations.append(
                    WouldBlock(self.name, name, violation.code, violation.message))
            return self._lenient(spec, raw, name)
        return value

    @staticmethod
    def _lenient(spec: Spec, raw: Any, name: str) -> Any:
        """Read the argument the way an unguarded tool would, taking the magnitude at
        face value and trusting the declared unit."""
        if spec.is_temporal:
            # An unguarded tool parses whatever timestamp it is handed and reads the
            # clock fields directly, so a naive value keeps its stated hour and an
            # offset-bearing one is not shifted into the tool's timezone.
            if isinstance(raw, str):
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return raw
            return raw
        if not spec.is_physical:
            return raw
        if isinstance(raw, Q):
            return raw
        magnitude = raw.get("value") if isinstance(raw, dict) else raw
        if isinstance(magnitude, str):
            try:
                magnitude = float(magnitude.split()[0])
            except (ValueError, IndexError):
                return raw
        if not isinstance(magnitude, (int, float)):
            return raw
        return Q(magnitude, spec.unit, datum=spec.datum)

    @staticmethod
    def _reject_carry_over(session, raw: Any, value: Q, field: str) -> None:
        found = session.detect_carry_over(raw, value)
        if found is None:
            return
        raise UnconvertedCarryOver(carry_over_message(raw, value, found), field=field)

    def _validate_result(self, result: Any) -> Any:
        if self.returns is None or self.enforcement == "off":
            return result
        if isinstance(self.returns, Spec):
            return self.returns.coerce(result, field="return")
        if not isinstance(result, dict):
            raise GuardViolation(
                f"{self.name} declares a mapping of return values, so it must return a dict",
                field="return",
            )
        return {
            key: self.returns[key].coerce(value, field=f"return.{key}")
            if key in self.returns
            else value
            for key, value in result.items()
        }

    @staticmethod
    def _iter_quantities(result: Any):
        if isinstance(result, Q):
            yield "return", result
        elif isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, Q):
                    yield key, value

    def clone(self, enforcement: str) -> GuardedTool:
        """The same tool under a different enforcement mode."""
        return GuardedTool(
            self.fn, dict(self.params), self.returns, self.name, self.description, enforcement
        )

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call from an agent, returning an MCP-shaped result rather than raising.

        A rejected call comes back as a tool error whose text tells the model what to
        send instead, which keeps the failure inside the conversation where it can be
        repaired.
        """
        try:
            result = self(**payload)
        except GuardViolation as violation:
            return violation.to_tool_error()
        except Exception as exc:  # pragma: no cover
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            }
        return {"isError": False, "result": self._serialise(result)}

    @staticmethod
    def _serialise(result: Any) -> Any:
        if isinstance(result, Q):
            return result.as_dict()
        if isinstance(result, dict):
            return {k: v.as_dict() if isinstance(v, Q) else v for k, v in result.items()}
        return result

    # Schema ----------------------------------------------------------------------------

    def json_schema(self, physical_metadata: bool = True) -> dict[str, Any]:
        """Tool definition in MCP shape, carrying the physical metadata extensions.

        Setting ``physical_metadata`` to False emits an ordinary schema with the units
        left in prose, which is what a tool written without this library looks like to a
        model. It exists to make the two forms comparable in evaluation.
        """
        required = [
            name
            for name, param in self.signature.parameters.items()
            if param.default is inspect.Parameter.empty
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        ]
        properties = {
            name: (
                self.params[name].json_schema()
                if name in self.params
                else {"description": f"{name}"}
            )
            for name in self.signature.parameters
            if self.signature.parameters[name].kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        if not physical_metadata:
            properties = {
                name: self._plain_property(name, prop) for name, prop in properties.items()
            }
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    def _plain_property(self, name: str, prop: dict[str, Any]) -> dict[str, Any]:
        """One parameter as an ordinary schema, with the unit demoted to prose."""
        spec = self.params.get(name)
        described = prop.get("description", "")
        if spec is not None and spec.unit:
            described = f"{described} In {spec.unit}.".strip()
        if spec is not None and spec.tz:
            return {"type": "string", "description": described}
        return {"type": "number", "description": described}


def quantity_tool(
    params: dict[str, Any] | None = None,
    returns: Any = None,
    *,
    name: str | None = None,
    description: str | None = None,
    enforcement: str = "strict",
):
    """Declare the physical types of a tool's parameters and result.

    >>> @quantity_tool(
    ...     params={"stage": {"unit": "ft", "datum": "NAVD88"},
    ...             "flood_stage": {"unit": "ft", "datum": "NAVD88"}},
    ...     returns={"unit": "ft"},
    ... )
    ... def freeboard(stage, flood_stage):
    ...     return flood_stage - stage
    """

    def decorate(fn: Callable) -> GuardedTool:
        return GuardedTool(fn, params or {}, returns, name, description, enforcement)

    return decorate
