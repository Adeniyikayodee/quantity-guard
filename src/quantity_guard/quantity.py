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
from .registry import normalize_quality, ureg, worst_quality

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
        return _np.asarray(value, dtype=float)
    return float(value)


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
                parsed = ureg.parse_units(self.units)
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
        """Build from a string such as ``"12.4 ft"``."""
        try:
            pq = ureg.Quantity(text)
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
        try:
            diff = self.pint - other.pint
        except pint.DimensionalityError as exc:
            raise DimensionalityError(
                f"cannot subtract {other:~} from {self:~}, {exc}"
            ) from None
        # Differencing two readings on one datum yields a delta, which carries no datum.
        datum = None if (self.datum and other.datum) else (self.datum or other.datum)
        return Q(
            diff.magnitude,
            diff.units,
            datum=datum,
            crs=self.crs or other.crs,
            quality=worst_quality(self.quality, other.quality),
        )

    def _scale(self, other: Any, op: str) -> Q:
        if isinstance(other, Q):
            if self.datum is not None or other.datum is not None:
                raise DatumMismatch(
                    f"cannot {op} absolute elevations, since the product of two values "
                    f"measured from a vertical reference has no meaning; difference them "
                    f"to deltas first"
                )
            right, quality = other.pint, worst_quality(self.quality, other.quality)
            crs = self.crs or other.crs
        else:
            right, quality, crs = other, self.quality, self.crs
        result = self.pint * right if op == "multiply" else self.pint / right
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
            "value": self.magnitude if self.is_scalar else self.magnitude.tolist(),
            "unit": format(self.units, "~"),
        }
        for key in ("datum", "crs", "quality", "source"):
            if getattr(self, key) is not None:
                payload[key] = getattr(self, key)
        return payload

    def __format__(self, spec: str) -> str:
        if not self.is_scalar:
            n = self.magnitude.size
            lo, hi = float(self.magnitude.min()), float(self.magnitude.max())
            body = f"[{n} values, {lo:g} to {hi:g}] {self.units:~P}"
        elif spec in ("", "~"):
            body = f"{self.magnitude:g} {self.units:~P}"
        else:
            body = format(self.pint, spec)
        tags = [t for t in (self.datum, self.quality) if t]
        return f"{body} ({', '.join(tags)})" if tags else body

    def __repr__(self) -> str:
        return f"Q({self:~})"
