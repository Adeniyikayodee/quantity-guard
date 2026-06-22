"""Tool decoration.

``quantity_tool`` wraps a function so that arguments are validated and normalised before
the body runs, results are validated before they leave, and both are written to the
provenance ledger. The body itself receives ``Q`` values and so is written in terms of
physical quantities rather than bare floats.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from .errors import GuardViolation
from .provenance import active_session
from .quantity import Q
from .spec import Spec


class GuardedTool:
    """A callable tool with declared physical types."""

    def __init__(
        self,
        fn: Callable,
        params: dict[str, Any],
        returns: Any = None,
        name: str | None = None,
        description: str | None = None,
    ):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = (description or inspect.getdoc(fn) or "").strip()
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
                value = spec.coerce(raw, field=name)
                bound.arguments[name] = value
                if session is not None and isinstance(value, Q):
                    session.record(self.name, "input", name, value)

        result = self.fn(*bound.args, **bound.kwargs)
        result = self._validate_result(result)

        if session is not None:
            for name, value in self._iter_quantities(result):
                session.record(self.name, "output", name, value)
        return result

    def _validate_result(self, result: Any) -> Any:
        if self.returns is None:
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
