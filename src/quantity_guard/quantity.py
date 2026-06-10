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
        object.__setattr__(self, "magnitude", float(self.magnitude))
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
    def dimensionality(self):
        return self.pint.dimensionality
