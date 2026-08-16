"""The quantity type carried across agent boundaries.

A ``Q`` is a magnitude with a unit, plus the reference metadata that determines whether
two dimensionally compatible values may legally be combined: vertical datum, coordinate
reference system, and record quality.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pint

from .errors import CRSMismatch, DatumMismatch, DimensionalityError, UnitParseError
from .registry import datums as _datums
from .registry import normalize_quality, normalize_unit_text, ureg, worst_quality

_normalise = normalize_unit_text

try:  # optional; only needed for series-valued quantities
    import numpy as _np
except ModuleNotFoundError:  # pragma: no cover - exercised only without numpy
    _np = None


def _magnitude(value: Any) -> Any:
    """A scalar float, or an array when the value is a sequence.

    Time series are the normal case in hydrology, so a quantity holds either one number
    or many. Everything downstream is written against pint, which handles both.
    """
    if _np is not None and isinstance(value, _np.ndarray):
        return value.astype(float)
    if isinstance(value, (list, tuple)) or hasattr(value, "__array__"):
        if _np is None:
            raise UnitParseError(
                "series-valued quantities need numpy; install quantity-guard[arrays]"
            )
        # A series keeps its gaps: NaN is how a time series marks a missing sample, and
        # dropping or refusing them would misrepresent the record. `as_dict` writes them
        # as null so the payload stays valid JSON.
        return _np.asarray(value, dtype=float)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        # A scalar has no gap to represent, so a non-finite one is a failed computation
        # or a sentinel that escaped its filter. Left alone it propagates through every
        # comparison as False and surfaces as "unsourced" rather than as invalid.
        raise UnitParseError(
            f"{value!r} is not a finite magnitude; a missing value should be omitted "
            f"rather than carried as NaN or infinity"
        )
    return number


def _offset_unit_error(op: str, left: Any, right: Any) -> DimensionalityError:
    """A guard violation for arithmetic on a degree scale.

    Water temperature is a first-class hydrology variable and the USGS pack maps straight
    onto ``degC``, but a degree Celsius is a point on a scale rather than an amount, so
    pint refuses to add or scale one. That refusal is correct; raising it as a bare pint
    error was not, because it carries no ``repair()`` text and escapes the tool-error path
    a model can act on.
    """
    shown = f"{left:~} and {right:~}" if isinstance(right, Q) else f"{left:~} by {right!r}"
    return DimensionalityError(
        f"cannot {op} {shown}: a temperature on a degree scale is a point on that scale, "
        f"not an amount of temperature, so it has no meaning under this operation. "
        f"Difference two temperatures to obtain an interval (which pint writes as "
        f"delta_degC), or convert to kelvin first"
    )


@dataclass(frozen=True)
class Q:
    """A physical quantity with reference metadata.

    ``datum`` marks the value as absolute against a named vertical reference. A value
    without a datum is either dimensionless of reference, such as a discharge, or a delta
    produced by differencing two readings on a common datum.
    """

    magnitude: float
    units: Any
    datum: str | None = None
    crs: str | None = None
    quality: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.units, str):
            try:
                parsed = ureg.parse_units(_normalise(self.units))
            except (pint.errors.UndefinedUnitError, pint.errors.DefinitionSyntaxError) as exc:
                raise UnitParseError(f"cannot parse unit {self.units!r}: {exc}") from None
            object.__setattr__(self, "units", parsed)
        object.__setattr__(self, "magnitude", _magnitude(self.magnitude))
        if self.datum is not None:
            _datums.get(self.datum)
        object.__setattr__(self, "quality", normalize_quality(self.quality))

    # Construction ----------------------------------------------------------------------

    @classmethod
    def parse(cls, text: str, **meta: Any) -> Q:
        """Build from a string such as ``"12.4 ft"``.

        The service spellings are accepted: ``"1250 ft3/s"`` is what NWIS publishes as a
        unit code and what models write in prose, and it means the same as ``ft**3/s``.
        """
        try:
            pq = ureg.Quantity(_normalise(text))
        except Exception as exc:
            raise UnitParseError(f"cannot parse quantity {text!r}: {exc}") from None
        if isinstance(pq, (int, float)) or pq.dimensionless:
            raise UnitParseError(f"{text!r} carries no unit")
        return cls(pq.magnitude, pq.units, **meta)

    @property
    def pint(self) -> pint.Quantity:
        return ureg.Quantity(self.magnitude, self.units)

    @property
    def is_scalar(self) -> bool:
        """False when this quantity holds a series rather than a single value."""
        return _np is None or not isinstance(self.magnitude, _np.ndarray)

    @property
    def dimensionality(self):
        return self.pint.dimensionality

    # Conversion ------------------------------------------------------------------------

    def to(self, unit: str | Any) -> Q:
        """Convert units, preserving all reference metadata."""
        try:
            converted = self.pint.to(unit)
        except pint.DimensionalityError as exc:
            raise DimensionalityError(
                f"cannot express {self:~} as {unit}, {exc}"
            ) from None
        return replace(self, magnitude=converted.magnitude, units=converted.units)

    def to_datum(self, target: str) -> Q:
        """Shift to another vertical datum using a registered local offset."""
        if self.datum is None:
            raise DatumMismatch(
                f"{self:~} carries no datum, so it cannot be shifted to {target!r}, "
                f"since only absolute elevations have a vertical reference"
            )
        offset_m = _datums.offset_metres(self.datum, target)
        shifted = self.pint + ureg.Quantity(offset_m, "meter").to(self.units)
        return replace(self, magnitude=shifted.magnitude, datum=target)

    # Arithmetic ------------------------------------------------------------------------

    def _check_frames(self, other: Q, op: str) -> None:
        if self.crs != other.crs and None not in (self.crs, other.crs):
            raise CRSMismatch(
                f"cannot {op} values in different coordinate reference systems "
                f"({self.crs} and {other.crs}), reproject first"
            )

    def __add__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        self._check_frames(other, "add")
        if self.datum is not None and other.datum is not None:
            raise DatumMismatch(
                f"cannot add two absolute elevations ({self.datum} and {other.datum}), "
                f"since their sum has no physical meaning; add a delta to an elevation "
                f"instead, or difference them to obtain one"
            )
        try:
            total = self.pint + other.pint
        except pint.DimensionalityError as exc:
            raise DimensionalityError(f"cannot add {self:~} to {other:~}, {exc}") from None
        except pint.errors.OffsetUnitCalculusError:
            raise _offset_unit_error("add", self, other) from None
        return Q(
            total.magnitude,
            total.units,
            datum=self.datum or other.datum,
            crs=self.crs or other.crs,
            quality=worst_quality(self.quality, other.quality),
        )

    def __sub__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        self._check_frames(other, "subtract")
        if self.datum != other.datum and None not in (self.datum, other.datum):
            raise DatumMismatch(
                f"cannot difference an elevation on {self.datum} against one on "
                f"{other.datum}, since both are in compatible units but measured from "
                f"different references; convert one with `.to_datum()` after registering "
                f"the local offset",
                left_datum=self.datum,
                right_datum=other.datum,
            )
        if self.datum is None and other.datum is not None:
            raise DatumMismatch(
                f"cannot subtract an elevation on {other.datum} from a value carrying no "
                f"datum, since the result is neither an elevation nor a delta; subtract "
                f"the delta from the elevation instead, or difference two elevations on "
                f"{other.datum} to obtain one",
                left_datum=self.datum,
                right_datum=other.datum,
            )
        try:
            diff = self.pint - other.pint
        except pint.DimensionalityError as exc:
            raise DimensionalityError(
                f"cannot subtract {other:~} from {self:~}, {exc}"
            ) from None
        except pint.errors.OffsetUnitCalculusError:
            raise _offset_unit_error("subtract", self, other) from None
        # Differencing two readings on one datum yields a delta, which carries no datum.
        # The reference can only come from the left operand: the right is either an
        # elevation being differenced away, or a delta that shifts the left one.
        datum = None if other.datum else self.datum
        return Q(
            diff.magnitude,
            diff.units,
            datum=datum,
            crs=self.crs or other.crs,
            quality=worst_quality(self.quality, other.quality),
        )

    def _scale(self, other: Any, op: str) -> Q:
        # Scaling is refused for anything carrying a datum, by a scalar as much as by
        # another quantity. Twice an elevation is not an elevation, and the result would
        # otherwise come back as a datum-free delta, which is the one shape that passes
        # every downstream check: `elevation * 1` would launder an absolute value past
        # the datum guard entirely.
        if self.datum is not None or (isinstance(other, Q) and other.datum is not None):
            raise DatumMismatch(
                f"cannot {op} a value measured from a vertical datum, since a multiple "
                f"of an absolute elevation has no physical meaning and the result would "
                f"no longer carry the reference it was measured from; difference it "
                f"against another elevation on the same datum to obtain a delta first"
            )
        if isinstance(other, Q):
            right, quality = other.pint, worst_quality(self.quality, other.quality)
            crs = self.crs or other.crs
        else:
            right, quality, crs = other, self.quality, self.crs
        try:
            result = self.pint * right if op == "multiply" else self.pint / right
        except pint.errors.OffsetUnitCalculusError:
            raise _offset_unit_error(op, self, other) from None
        return Q(result.magnitude, result.units, crs=crs, quality=quality)

    def __mul__(self, other: Any) -> Q:
        return self._scale(other, "multiply")

    __rmul__ = __mul__

    def __truediv__(self, other: Any) -> Q:
        return self._scale(other, "divide")

    # Comparison ------------------------------------------------------------------------

    def _comparable(self, other: Q) -> tuple[float, float]:
        if not isinstance(other, Q):
            raise TypeError(f"cannot compare Q against {type(other).__name__}")
        if self.datum != other.datum:
            raise DatumMismatch(
                f"cannot compare a value on {self.datum} against one on {other.datum}, "
                f"since the comparison is only meaningful on a shared reference"
            )
        try:
            return self.pint.magnitude, other.pint.to(self.units).magnitude
        except pint.DimensionalityError as exc:
            raise DimensionalityError(f"cannot compare {self:~} with {other:~}, {exc}") from None

    def __lt__(self, other: Q) -> bool:
        left, right = self._comparable(other)
        return left < right

    def __le__(self, other: Q) -> bool:
        left, right = self._comparable(other)
        return left <= right

    def __gt__(self, other: Q) -> bool:
        left, right = self._comparable(other)
        return left > right

    def __ge__(self, other: Q) -> bool:
        left, right = self._comparable(other)
        return left >= right

    # Serialisation ---------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            # A gap in a series is written as null. `NaN` is what json.dumps emits
            # otherwise, and it is not valid JSON, so the payload would be rejected by a
            # conforming parser at the other end of the tool call.
            "value": self.magnitude if self.is_scalar else [
                None if v != v else v for v in self.magnitude.tolist()
            ],
            "unit": format(self.units, "~"),
        }
        for key in ("datum", "crs", "quality", "source"):
            if getattr(self, key) is not None:
                payload[key] = getattr(self, key)
        return payload

    def __format__(self, spec: str) -> str:
        if not self.is_scalar:
            n = self.magnitude.size
            gaps = int(_np.count_nonzero(_np.isnan(self.magnitude)))
            if gaps == n:
                body = f"[{n} values, all missing] {self.units:~P}"
            else:
                # Ranged over the samples that exist, so one gap does not render the
                # whole series as "nan to nan".
                lo = float(_np.nanmin(self.magnitude))
                hi = float(_np.nanmax(self.magnitude))
                body = f"[{n} values, {lo:g} to {hi:g}] {self.units:~P}"
                if gaps:
                    body = f"[{n} values, {lo:g} to {hi:g}, {gaps} missing] {self.units:~P}"
        elif spec in ("", "~"):
            body = f"{self.magnitude:g} {self.units:~P}"
        else:
            body = format(self.pint, spec)
        tags = [t for t in (self.datum, self.quality) if t]
        return f"{body} ({', '.join(tags)})" if tags else body

    def __repr__(self) -> str:
        return f"Q({self:~})"
