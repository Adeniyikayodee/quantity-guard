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
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pint

from .quantity import Q
from .registry import ureg

# A ContextVar rather than a module-level list, so concurrent agent runs in the same
# process keep separate ledgers. Async agents interleave, and a shared stack would
# attribute one run's tool outputs to another's answer audit.
_ACTIVE: ContextVar[tuple["Session", ...]] = ContextVar("quantity_guard_sessions", default=())


def active_session() -> "Session | None":
    stack = _ACTIVE.get()
    return stack[-1] if stack else None


@dataclass
class LedgerEntry:
    tool: str
    role: str  # "input", "output", or "derived"
    field: str
    quantity: Q
    call_id: int
    note: str = ""


@dataclass
class WouldBlock:
    """A call that ``warn`` enforcement let through and ``strict`` would have rejected."""

    tool: str
    field: str
    code: str
    message: str


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
    #: "sourced", "derived", "quoted", "unsourced", "unit_mislabelled", or "ignored"
    status: str
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

    @property
    def derived(self) -> list[NumberClaim]:
        """Traceable to an arithmetic combination of recorded outputs."""
        return [c for c in self.claims if c.status == "derived"]

    @property
    def quoted(self) -> list[NumberClaim]:
        """Repeated back from the question rather than asserted as a measurement."""
        return [c for c in self.claims if c.status == "quoted"]

    @property
    def ok(self) -> bool:
        return not self.unsourced and not self.mislabelled

    def report(self) -> str:
        lines = []
        for claim in self.claims:
            if claim.status == "ignored":
                continue
            mark = {
                "sourced": "ok", "derived": "derived", "quoted": "quoted",
                "unsourced": "UNSOURCED", "unit_mislabelled": "MISLABELLED",
            }[claim.status]
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
    # "as" is a valid pint unit (attoseconds) and never means that in prose.
    "as", "so", "if", "we", "it", "its", "that", "this", "then", "was", "were",
}


@dataclass
class Session:
    """Records quantities crossing tool boundaries within a block of agent work."""

    entries: list[LedgerEntry] = field(default_factory=list)
    calls: int = 0
    #: The question this block of work answers. Numbers the asker supplied are legitimate
    #: inputs, so they count as a source alongside the tools.
    context: str = ""
    #: Calls a tool in ``warn`` mode let through, which ``strict`` would have rejected.
    violations: list[WouldBlock] = field(default_factory=list)
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

    @property
    def scalar_outputs(self) -> list[LedgerEntry]:
        """Outputs a single number can be compared against.

        Carry-over detection and the answer audit both match one magnitude at a time, so
        a series-valued output is recorded for the manifest but never matched.
        """
        return [e for e in self.outputs if e.quantity.is_scalar]

    def detect_carry_over(self, raw: Any, coerced: Q) -> CarryOver | None:
        """Find a prior output whose magnitude was reused without its reference frame.

        The signature is a magnitude that survives a conversion unchanged. If the value
        this tool ends up with equals one an earlier tool returned, while the two are on
        different footings, no conversion was applied. That covers a unit dropped from a
        bare number and a unit asserted wrongly in its place, which are the same mistake
        arriving by different routes.

        Two frames are checked. A dropped unit gives a wrong answer by some ratio, and a
        dropped datum gives a wrong answer by an offset, which is the harder of the two
        to notice because the result stays plausible.
        """
        if not coerced.is_scalar:
            return None
        # Keyed on the value the tool will act on, not on how it was written. A bare
        # number and an explicitly mislabelled one reach the same wrong magnitude.
        held = float(coerced.magnitude)
        for entry in self.scalar_outputs:
            prior = entry.quantity
            if abs(prior.magnitude - held) > 1e-9 * max(1.0, abs(prior.magnitude)):
                continue
            # A datum is only dropped if this parameter declares one to drop it against.
            if (coerced.datum is not None and prior.datum is not None
                    and prior.datum != coerced.datum):
                return CarryOver(entry, "datum")
            try:
                converted = prior.pint.to(coerced.units).magnitude
            except Exception:
                continue
            # Equal magnitudes are only suspicious when a conversion was actually
            # required. Aliases of one unit (cms and m**3/s), and values that are
            # invariant under the conversion such as zero, convert to themselves and
            # are correct as sent.
            if abs(converted - prior.magnitude) <= 1e-9 * max(1.0, abs(prior.magnitude)):
                continue
            return CarryOver(entry, "unit")
        return None

    def traces(self, quantity: Q, tolerance: float = 0.005) -> bool:
        """Whether a value could have come from a tool, a derivation, or the question.

        Used to check an input before it is acted on, rather than an answer after the
        fact. A figure that traces to nothing entered the conversation from the model's
        own memory.
        """
        if not quantity.is_scalar:
            return True
        value = float(quantity.magnitude)
        for entry in self.scalar_outputs:
            converted = self._convert(entry.quantity, quantity.units)
            if converted is not None and self._close(value, converted, tolerance):
                return True
        for candidate in self._derivable():
            try:
                magnitude = candidate.to(quantity.units).magnitude
            except Exception:
                continue
            if self._close(value, magnitude, tolerance):
                return True
        return any(self._close(value, number, tolerance)
                   for number in self._context_numbers())

    def _context_numbers(self) -> set[float]:
        return {float(m.group("num").replace(",", ""))
                for m in _NUMBER.finditer(self.context)}

    # Auditing --------------------------------------------------------------------------

    def audit_answer(self, text: str, tolerance: float = 0.005,
                     context: str = "") -> AnswerAudit:
        """Check every numeric literal in ``text`` against recorded tool outputs.

        ``context`` is the question the answer responds to. Numbers repeated back from it
        are quoted rather than asserted, and flagging them as unsupported measurements is
        noise: a horizon of "36 hours" came from the asker, not from the model.
        """
        quoted = {float(m.group("num").replace(",", ""))
                  for m in _NUMBER.finditer(context)} if context else set()
        derived = self._derivable()
        claims = [
            self._judge(match, tolerance, quoted, derived)
            for match in _NUMBER.finditer(text)
        ]
        return AnswerAudit(claims=[c for c in claims if c is not None])

    def _derivable(self, depth: int = 1, cap: int = 400) -> list[Any]:
        """Quantities reachable by adding or subtracting recorded outputs.

        A model that adds a station datum to a stage, or differences two elevations, is
        reporting a traceable number even though no tool returned it.

        Only sums and differences of like dimensions are followed. Allowing products and
        quotients as well was measured to accept 53% of randomly chosen numbers on a
        six-output ledger, which would leave the audit unable to detect anything; those
        are also the operations a model delegates to a tool rather than doing by hand.
        Physical legality is the tool boundary's concern, so the arithmetic here ignores
        datums and works on raw magnitudes.
        """
        base = [e.quantity.pint for e in self.scalar_outputs][:12]
        reached: list[Any] = list(base)
        for _ in range(depth):
            fresh: list[Any] = []
            for left in reached:
                for right in base:
                    if left.dimensionality != right.dimensionality:
                        continue
                    if len(reached) + len(fresh) > cap:
                        break
                    try:
                        fresh.append(left + right)
                        fresh.append(left - right)
                    except Exception:
                        continue
            reached += fresh
        return reached

    def _judge(self, match: re.Match, tolerance: float,
               quoted: set[float] | None = None,
               derived: list[Any] | None = None) -> NumberClaim | None:
        raw = match.group("num")
        value = float(raw.replace(",", ""))
        written = raw.split(".")[1] if "." in raw else ""
        decimals = len(written)
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

        if self._is_ignorable(value, unit_token, raw, match):
            return NumberClaim(text=text, value=value, unit=unit_token or None, status="ignored")

        magnitude_match: LedgerEntry | None = None
        for entry in self.scalar_outputs:
            quantity = entry.quantity
            if unit is not None:
                converted = self._convert(quantity, unit)
                if converted is not None and self._close_either_sign(
                        value, converted, tolerance, decimals):
                    return NumberClaim(
                        text=text, value=value, unit=unit_token, status="sourced", matched=entry,
                        detail=f"from {entry.tool}.{entry.field}",
                    )
            if self._close_either_sign(value, quantity.magnitude, tolerance, decimals):
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

        if quoted and any(self._close(value, q, tolerance, decimals) for q in quoted):
            return NumberClaim(
                text=text, value=value, unit=unit_token or None, status="quoted",
                detail="repeated back from the question",
            )

        for candidate in derived or []:
            magnitude = candidate.magnitude
            if unit is not None:
                try:
                    magnitude = candidate.to(unit).magnitude
                except Exception:
                    continue
            if self._close_either_sign(value, magnitude, min(tolerance, 0.001), decimals):
                return NumberClaim(
                    text=text, value=value, unit=unit_token or None, status="derived",
                    detail="an arithmetic combination of recorded outputs",
                )

        return NumberClaim(
            text=text, value=value, unit=unit_token or None, status="unsourced",
            detail="no tool output produced this value",
        )

    @staticmethod
    def _is_ignorable(value: float, unit_token: str, raw: str,
                      match: re.Match | None = None) -> bool:
        """Whether a bare number is prose rather than a measurement.

        A quantity stated in an answer almost always carries a unit or a decimal part.
        The remaining bare integers are dominated by identifiers, calendar years, and
        small counts, so auditing them produces noise rather than findings.
        """
        if unit_token:
            return False
        # A hyphenated compound is a modifier, not a measurement: "a 50-year-old survey"
        # describes the survey, it does not assert fifty of anything.
        if match is not None:
            after = match.string[match.end():match.end() + 2]
            if after[:1] == "-" and after[1:2].isalpha():
                return True
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

    @classmethod
    def _close_either_sign(cls, candidate: float, reference: float, tolerance: float,
                           decimals: int | None = None) -> bool:
        """Match a magnitude regardless of which side carries the sign.

        A tool returns a datum offset of -0.44 ft and the model writes "subtract 0.44 ft".
        The sign has moved into the prose, and the figure is still the retrieved one.
        Provenance asks where a magnitude came from, so the comparison ignores it.
        """
        return (cls._close(candidate, reference, tolerance, decimals)
                or cls._close(candidate, -reference, tolerance, decimals))

    @staticmethod
    def _close(candidate: float, reference: float, tolerance: float,
               decimals: int | None = None) -> bool:
        if reference == 0:
            return abs(candidate) < 1e-12
        if abs(candidate - reference) / abs(reference) <= tolerance:
            return True
        # A rounded restatement of the same value, as in 14.23 written as 14.2, or 17.1
        # written as 17. The precision is taken from the literal as written, since the
        # repr of the parsed float implies a precision the author did not state.
        if decimals is None:
            decimals = len(str(candidate).split(".")[1]) if "." in str(candidate) else 0
        return round(reference, decimals) == round(candidate, decimals)

    def enforcement_report(self) -> str:
        """What switching enforcement on would have changed.

        ``warn`` mode exists so a team can measure the cost of enforcement before paying
        it. This is that measurement: the calls that would have been rejected, grouped by
        the check that would have rejected them.
        """
        if not self.violations:
            return (
                f"No calls would have been blocked across {self.calls} tool "
                f"call{'s' if self.calls != 1 else ''}."
            )
        by_code: dict[str, list[WouldBlock]] = {}
        for entry in self.violations:
            by_code.setdefault(entry.code, []).append(entry)

        lines = [
            f"{len(self.violations)} of {self.calls} tool calls would have been blocked:"
        ]
        for code, group in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {len(group)}x {code}")
            for entry in group[:3]:
                lines.append(f"      {entry.tool}.{entry.field}: {entry.message}")
            if len(group) > 3:
                lines.append(f"      ... and {len(group) - 3} more")
        return "\n".join(lines)

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
def session(context: str = "") -> Iterator[Session]:
    """Open a recording scope, within which guarded tools log their quantities.

    ``context`` is the question being answered. Supplying it lets the ledger tell a
    number the asker gave from one the model invented.
    """
    current = Session(context=context)
    token = _ACTIVE.set(_ACTIVE.get() + (current,))
    try:
        yield current
    finally:
        _ACTIVE.reset(token)


def _written(raw: Any) -> str:
    """How the value was sent, for an error the model has to act on."""
    if isinstance(raw, dict):
        return f"{raw.get('value')} labelled {raw.get('unit')}"
    if isinstance(raw, str):
        return repr(raw)
    return f"the bare number {raw:g}"


def carry_over_message(raw: Any, value: Q, found: CarryOver) -> str:
    """Explain a dropped or wrongly asserted reference frame, with the fix to send."""
    held = f"{float(value.magnitude):g}"
    prior, entry = found.entry.quantity, found.entry
    source = f"{entry.tool}.{entry.field}"
    if found.reason == "datum":
        return (
            f"received {_written(raw)}, which this parameter reads as "
            f"{value.magnitude:g} {value.units:~P} on {value.datum}, but {source} "
            f"returned it on {prior.datum}. These are measured from different "
            f"references, so the "
            f"magnitude cannot be reused; convert it to {value.datum} first, or resend "
            f'it as {{"value": {held}, "datum": "{prior.datum}"}}'
        )
    return (
        f"received {_written(raw)}, which this tool reads as {value:~}, but "
        f"{source} returned {prior:~} and no conversion was applied; resend the value "
        f'with its original unit, as {{"value": {held}, '
        f'"unit": "{format(prior.units, "~")}"}}'
    )
