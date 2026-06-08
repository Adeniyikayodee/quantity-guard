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
