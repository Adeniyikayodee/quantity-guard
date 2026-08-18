"""Guard violations.

Each violation carries a ``repair()`` string written for a model rather than a human,
since that string is what re-enters the conversation when a guarded tool rejects a call,
and it has to state both what was wrong and what to send instead.
"""

from __future__ import annotations

from typing import Any


class GuardViolation(Exception):
    """Base class for the checks quantity-guard enforces."""

    code = "guard_violation"

    def __init__(self, message: str, *, field: str | None = None, **context: Any):
        super().__init__(message)
        self.message = message
        self.field = field
        self.context = context

    def repair(self) -> str:
        """Instruction for fixing the call, addressed to the calling model."""
        where = f" for `{self.field}`" if self.field else ""
        return f"[{self.code}]{where} {self.message}"

    def to_tool_error(self) -> dict[str, Any]:
        """MCP-shaped error result, so the agent sees a failure it can repair."""
        return {
            "isError": True,
            "content": [{"type": "text", "text": self.repair()}],
            "code": self.code,
            "field": self.field,
        }

    def __str__(self) -> str:  # pragma: no cover
        where = f" ({self.field})" if self.field else ""
        return f"{self.message}{where}"


class MissingUnit(GuardViolation):
    """A bare number arrived where the tool requires an explicit unit."""

    code = "missing_unit"


class UnitParseError(GuardViolation):
    code = "unit_parse_error"


class InvalidArguments(GuardViolation):
    """The call does not fit the tool's signature at all.

    Raised before any physical check, since an argument the tool does not take, or a
    required one left out, is a mistake about the tool rather than about a quantity. It
    is reported in the same shape as every other violation so a model handling tool
    errors by ``code`` sees one vocabulary.
    """

    code = "invalid_arguments"


class DimensionalityError(GuardViolation):
    """The value is the wrong kind of physical quantity, such as a length where a
    volumetric flow rate is required."""

    code = "dimensionality_error"


class DatumMismatch(GuardViolation):
    """Dimensionally compatible but semantically incompatible, which is the error class
    `pint` cannot detect, as in two elevations expressed in feet against different
    vertical references."""

    code = "datum_mismatch"


class DatumConversionUnavailable(GuardViolation):
    """No offset is registered between the two datums.

    The conversion is refused rather than guessed, because NAVD88 to NGVD29 varies with
    location (VERTCON) and any constant offset would be quietly wrong across most of the
    domain.
    """

    code = "datum_conversion_unavailable"


class CRSMismatch(GuardViolation):
    code = "crs_mismatch"


class TimezoneError(GuardViolation):
    """A naive timestamp, or one outside the timezone the tool declares."""

    code = "timezone_error"


class QualityViolation(GuardViolation):
    """The result rests on record too provisional for the tool's stated requirement."""

    code = "quality_violation"


class UnconvertedCarryOver(GuardViolation):
    """A magnitude was carried from one tool to another without its unit.

    The incoming bare number equals a value a previous tool returned in a different but
    dimensionally compatible unit, which indicates the conversion step was skipped.
    """

    code = "unconverted_carry_over"


class UnsourcedInput(GuardViolation):
    """A value entered a tool having come from neither a tool nor the question.

    Distinct from :class:`UnsourcedNumber`, which is about the final answer. Catching it
    at the input is what stops an invented figure from being laundered into a computed
    result the audit would then call sourced.
    """

    code = "unsourced_input"


class UnsourcedNumber(GuardViolation):
    """A number in the final answer traces to no recorded tool output."""

    code = "unsourced_number"
