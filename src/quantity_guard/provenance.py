"""Provenance ledger and answer auditing.

Guarded tools record every quantity that crosses their boundary. Auditing an answer then
asks a narrow question of each number in the text: does it trace to something a tool
actually returned, and is it stated in the unit it was returned in.

This addresses the failure mode in which a model bypasses the tool path and produces a
plausible figure directly, which conventional output validation cannot detect because the
value is well-formed.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pint

from .quantity import Q
from .registry import ureg

_ACTIVE: list["Session"] = []


def active_session() -> "Session | None":
    return _ACTIVE[-1] if _ACTIVE else None


@dataclass
class LedgerEntry:
    tool: str
    role: str  # "input", "output", or "derived"
    field: str
    quantity: Q
    call_id: int
    note: str = ""


@dataclass
class CarryOver:
    """A prior output whose magnitude reappeared without its reference frame."""

    entry: LedgerEntry
    reason: str  # "unit" or "datum"


@dataclass
class NumberClaim:
    """A numeric literal found in an answer, with the verdict of the audit."""

    text: str
    value: float
    unit: str | None
    status: str  # "sourced", "unsourced", "unit_mislabelled", or "ignored"
    matched: LedgerEntry | None = None
    detail: str = ""


@dataclass
class AnswerAudit:
    claims: list[NumberClaim]

    @property
    def unsourced(self) -> list[NumberClaim]:
        return [c for c in self.claims if c.status == "unsourced"]

    @property
    def mislabelled(self) -> list[NumberClaim]:
        return [c for c in self.claims if c.status == "unit_mislabelled"]

    def report(self) -> str:
        lines = []
        for claim in self.claims:
            if claim.status == "ignored":
                continue
            mark = {"sourced": "ok", "unsourced": "UNSOURCED",
                    "unit_mislabelled": "MISLABELLED"}[claim.status]
            lines.append(f"  [{mark}] {claim.text}  {claim.detail}")
        return "\n".join(lines) or "  (no numeric claims found)"


# Number followed by an optional unit token. A unit is a word with an optional exponent,
# optionally divided by a second such word, which covers the forms that occur in prose:
# cfs, m3/s, m**3/s, m³/s, mm/day, degC.
_EXPONENT = r"(?:\*\*-?\d+|\^-?\d+|[\d²³])?"
_WORD = r"[A-Za-zµ°]+" + _EXPONENT
# The lookbehind keeps digits embedded in names out of the audit, so NAVD88 and NGVD29
# are read as datum names rather than as the measurements 88 and 29.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<num>-?\d[\d,]*(?:\.\d+)?)"
    rf"(?:\s*(?P<unit>%|{_WORD}(?:\s*/\s*{_WORD})?))?"
)

# Words that follow a number in prose without being units. Duration words are absent
# deliberately, since a stated forecast horizon is a claim like any other and is audited.
_NOT_UNITS = {
    "and", "or", "of", "to", "the", "a", "an", "at", "in", "on", "is", "was", "for",
    "from", "by", "with", "per", "about", "above", "below", "over", "under", "than",
    "am", "pm", "utc", "gage", "gauge", "station", "site", "times", "x", "no", "not",
}


@dataclass
class Session:
    """Records quantities crossing tool boundaries within a block of agent work."""

    entries: list[LedgerEntry] = field(default_factory=list)
    calls: int = 0
    started: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def record(self, tool: str, role: str, name: str, quantity: Q, note: str = "") -> None:
        self.entries.append(
            LedgerEntry(tool=tool, role=role, field=name, quantity=quantity, call_id=self.calls, note=note)
        )

    def record_derived(self, quantity: Q, note: str = "") -> None:
        """Register a value computed outside a guarded tool so the audit accepts it."""
        self.record("(derived)", "derived", note or "value", quantity, note)

    @property
    def outputs(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.role in ("output", "derived")]

    def detect_carry_over(self, raw: Any, coerced: Q) -> CarryOver | None:
        """Find a prior output whose magnitude was reused without its reference frame.

        A bare number entering a tool is read in that tool's declared unit and datum.
        When it also equals a value an earlier tool returned on a different footing, the
        conversion was almost certainly skipped, which is the arithmetic behind most
        order-of-magnitude errors in agent transcripts.

        """
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        for entry in self.outputs:
            prior = entry.quantity
            if abs(prior.magnitude - float(raw)) > 1e-9 * max(1.0, abs(prior.magnitude)):
                continue
            if prior.units == coerced.units:
                continue
            try:
                prior.pint.to(coerced.units)
            except Exception:
                continue
            return CarryOver(entry, "unit")
        return None

    # Auditing --------------------------------------------------------------------------

    def audit_answer(self, text: str, tolerance: float = 0.005) -> AnswerAudit:
        """Check every numeric literal in ``text`` against recorded tool outputs.
        """
        claims = [
            self._judge(match, tolerance)
            for match in _NUMBER.finditer(text)
        ]
        return AnswerAudit(claims=[c for c in claims if c is not None])

    def _judge(self, match: re.Match, tolerance: float) -> NumberClaim | None:
        raw = match.group("num")
        value = float(raw.replace(",", ""))
        unit_token = (match.group("unit") or "").strip()
        if unit_token.lower() in _NOT_UNITS:
            unit_token = ""
        text = match.group(0).strip()

        # A trailing word that is not a recognised unit is prose, not a measurement, so
        # the number is judged as bare.
        unit = self._parse_unit(unit_token) if unit_token else None
        if unit is None:
            unit_token = ""
            text = raw

        if self._is_ignorable(value, unit_token, raw):
            return NumberClaim(text=text, value=value, unit=unit_token or None, status="ignored")

        magnitude_match: LedgerEntry | None = None
        for entry in self.outputs:
            quantity = entry.quantity
            if unit is not None:
                converted = self._convert(quantity, unit)
                if converted is not None and self._close(value, converted, tolerance):
                    return NumberClaim(
                        text=text, value=value, unit=unit_token, status="sourced", matched=entry,
                        detail=f"from {entry.tool}.{entry.field}",
                    )
            if self._close(value, quantity.magnitude, tolerance):
                magnitude_match = magnitude_match or entry
                if unit is None:
                    return NumberClaim(
                        text=text, value=value, unit=None, status="sourced", matched=entry,
                        detail=f"from {entry.tool}.{entry.field}",
                    )

        if magnitude_match is not None and unit is not None:
            native = format(magnitude_match.quantity.units, "~")
            return NumberClaim(
                text=text, value=value, unit=unit_token, status="unit_mislabelled",
                matched=magnitude_match,
                detail=(
                    f"magnitude matches {magnitude_match.tool}.{magnitude_match.field} "
                    f"but that value is in {native}, not {unit_token}"
                ),
            )

        return NumberClaim(
            text=text, value=value, unit=unit_token or None, status="unsourced",
            detail="no tool output produced this value",
        )

    @staticmethod
    def _is_ignorable(value: float, unit_token: str, raw: str) -> bool:
        """Whether a bare number is prose rather than a measurement.

        A quantity stated in an answer almost always carries a unit or a decimal part.
        The remaining bare integers are dominated by identifiers, calendar years, and
        small counts, so auditing them produces noise rather than findings.
        """
        if unit_token:
            return False
        digits = raw.lstrip("-").replace(",", "")
        if "." in digits:
            return False
        # Station and site numbers, which are zero-padded or simply long.
        if (digits.startswith("0") and len(digits) > 1) or len(digits) >= 5:
            return True
        if value.is_integer():
            # Calendar years, and small counts such as list positions or station counts.
            return 1800 <= value <= 2100 or abs(value) <= 31
        return False

    @staticmethod
    def _parse_unit(token: str):
        normalised = token.replace("²", "2").replace("³", "3").replace("^", "**")
        # Prose writes m3/s where pint expects m**3/s.
        normalised = re.sub(r"(?<![*\d])([A-Za-zµ°])(\d)", r"\1**\2", normalised)
        try:
            unit = ureg.parse_units(normalised)
        except Exception:
            return None
        return None if not str(unit) else unit

    @staticmethod
    def _convert(quantity: Q, unit) -> float | None:
        try:
            return quantity.pint.to(unit).magnitude
        except (pint.DimensionalityError, pint.errors.UndefinedUnitError):
            return None

    @staticmethod
    def _close(candidate: float, reference: float, tolerance: float) -> bool:
        if reference == 0:
            return abs(candidate) < 1e-12
        if abs(candidate - reference) / abs(reference) <= tolerance:
            return True
        # A rounded restatement of the same value, as in 14.23 written as 14.2.
        decimals = len(str(candidate).split(".")[1]) if "." in str(candidate) else 0
        return round(reference, decimals) == round(candidate, decimals)

    # Reproducibility -------------------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        """Session record sufficient to re-run and check the numbers."""
        return {
            "started": self.started,
            "calls": self.calls,
            "quantities": [
                {
                    "tool": e.tool,
                    "role": e.role,
                    "field": e.field,
                    "call_id": e.call_id,
                    **e.quantity.as_dict(),
                }
                for e in self.entries
            ],
        }


@contextmanager
def session() -> Iterator[Session]:
    """Open a recording scope, within which guarded tools log their quantities."""
    current = Session()
    _ACTIVE.append(current)
    try:
        yield current
    finally:
        _ACTIVE.pop()


def carry_over_message(raw: Any, value: Q, found: CarryOver) -> str:
    """Explain a dropped unit in terms the model can act on."""
    prior, entry = found.entry.quantity, found.entry
    source = f"{entry.tool}.{entry.field}"
    return (
        f"received the bare number {raw:g}, which this tool reads as {value:~}, but "
        f"{source} returned {prior:~} and no conversion was applied; resend the value "
        f'with its original unit, as {{"value": {raw:g}, '
        f'"unit": "{format(prior.units, "~")}"}}'
    )
