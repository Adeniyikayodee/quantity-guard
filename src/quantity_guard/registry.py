"""Unit and datum registries.

`pint` supplies dimensional analysis, while the datum registry supplies what `pint`
structurally cannot: reference frames that share a unit without sharing a meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pint

from .errors import DatumConversionUnavailable, DatumMismatch

# Process-wide singleton, since quantities built from different pint registries cannot
# interoperate.
ureg = pint.UnitRegistry()

# Units in common engineering and hydrology use that pint does not ship.
_EXTRA_UNITS = [
    "cfs = foot ** 3 / second",
    "cms = meter ** 3 / second",
    "mgd = 1e6 * gallon / day",
    "sfd = foot ** 3 / second * day",
    # Apparent and reactive power share watt dimensions but are named apart
    # in power engineering, and the distinction has to survive a round trip.
    "VA = volt * ampere",
    "var = volt * ampere",
]

for _defn in _EXTRA_UNITS:
    try:
        ureg.define(_defn)
    except (pint.errors.RedefinitionError, pint.errors.DefinitionSyntaxError):
        pass


# Quality flags -------------------------------------------------------------------------

# Higher rank means less trustworthy. Combining quantities takes the worst known rank, so
# any computation touching provisional record yields a provisional answer.
QUALITY_RANK: dict[str, int] = {
    "approved": 0,
    "reviewed": 0,
    "estimated": 1,
    "provisional": 2,
    "unverified": 3,
}

# Single-letter codes as published on USGS time series.
QUALITY_ALIASES: dict[str, str] = {
    "A": "approved",
    "R": "reviewed",
    "e": "estimated",
    "E": "estimated",
    "P": "provisional",
    "p": "provisional",
}


def normalize_quality(flag: str | None) -> str | None:
    if flag is None:
        return None
    flag = QUALITY_ALIASES.get(flag, str(flag).strip().lower())
    if flag not in QUALITY_RANK:
        raise ValueError(f"unknown quality flag {flag!r}, known: {sorted(QUALITY_RANK)}")
    return flag


def worst_quality(*flags: str | None) -> str | None:
    known = [f for f in flags if f is not None]
    return max(known, key=lambda f: QUALITY_RANK[f]) if known else None


# Datums --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Datum:
    """A measurement reference frame.

    Absolute datums describe position relative to a fixed reference, such as an elevation
    above NAVD88. The difference between two absolute readings on the same datum is a
    delta carrying no datum, which is what makes freeboard arithmetic well-defined.
    """

    name: str
    kind: str = "vertical"
    description: str = ""


@dataclass
class DatumRegistry:
    datums: dict[str, Datum] = field(default_factory=dict)
    # (from, to) -> offset in metres, defined so that value_to = value_from + offset
    _offsets: dict[tuple[str, str], float] = field(default_factory=dict)

    def register(self, name: str, kind: str = "vertical", description: str = "") -> Datum:
        datum = Datum(name=name, kind=kind, description=description)
        self.datums[name] = datum
        return datum

    def get(self, name: str) -> Datum:
        if name not in self.datums:
            raise DatumMismatch(
                f"unknown datum {name!r}, register it with `datums.register({name!r})` "
                f"or use one of {sorted(self.datums) or '(none registered)'}"
            )
        return self.datums[name]

    def register_offset(self, from_datum: str, to_datum: str, offset) -> None:
        """Declare that ``value_to = value_from + offset``.

        Offsets are explicit and local by design, since a gage datum offset is valid at
        one station, and a VERTCON offset is valid at one point, so nothing here is
        inferred from context.
        """
        self.get(from_datum)
        self.get(to_datum)
        metres = float(offset.to("meter").magnitude) if hasattr(offset, "to") else float(offset)
        self._offsets[(from_datum, to_datum)] = metres
        self._offsets[(to_datum, from_datum)] = -metres

    def offset_metres(self, from_datum: str, to_datum: str) -> float:
        if from_datum == to_datum:
            return 0.0
        try:
            return self._offsets[(from_datum, to_datum)]
        except KeyError:
            raise DatumConversionUnavailable(
                f"no registered offset from {from_datum!r} to {to_datum!r}. This "
                f"conversion depends on location and will not be guessed, so obtain the "
                f"local offset (VERTCON for NAVD88 and NGVD29, the published station "
                f"datum for a gage) and register it with "
                f"`datums.register_offset({from_datum!r}, {to_datum!r}, offset)`",
                from_datum=from_datum,
                to_datum=to_datum,
            ) from None

    def can_convert(self, from_datum: str, to_datum: str) -> bool:
        return from_datum == to_datum or (from_datum, to_datum) in self._offsets


datums = DatumRegistry()

# Vertical datums in common use in US water work.
datums.register("NAVD88", description="North American Vertical Datum of 1988")
datums.register("NGVD29", description="National Geodetic Vertical Datum of 1929")
datums.register("GAGE", description="Local gage datum, station-specific offset required")
datums.register("MSL", description="Mean sea level, station-dependent")
datums.register("MLLW", description="Mean lower low water")
