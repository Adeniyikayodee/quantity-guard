"""Unit and datum registries.

`pint` supplies dimensional analysis, while the datum registry supplies what `pint`
structurally cannot: reference frames that share a unit without sharing a meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pint

from .errors import DatumConversionUnavailable, DatumMismatch, QualityViolation

# Process-wide singleton, since quantities built from different pint registries cannot
# interoperate.
ureg = pint.UnitRegistry()

# Units in common engineering and hydrology use that pint does not ship.
_EXTRA_UNITS = [
    "cfs = foot ** 3 / second",
    "cms = meter ** 3 / second",
    # Commonwealth spoken forms for the same two units. Models write them in prose and
    # the UK suite's own vocabulary uses them, so leaving them undefined made a correctly
    # converted answer ("35.4 cumecs") read as unsourced.
    "cumec = meter ** 3 / second",
    "cusec = foot ** 3 / second",
    # Million gallons per day, on the US liquid gallon, which is what USGS publishes.
    # The imperial form is a fifth larger and is named apart: UK water-resources practice
    # writes "mgd" for million *imperial* gallons per day, and reading one as the other
    # is a 20% error in a licensed abstraction. Neither spelling is allowed to stand for
    # both, so a caller has to say which.
    "us_mgd = 1e6 * gallon / day = mgd",
    "imperial_mgd = 1e6 * imperial_gallon / day",
    "sfd = foot ** 3 / second * day",
    # Real, apparent, and reactive power are all volt-amperes dimensionally, and are not
    # interchangeable: converting between them needs a power factor, which is a property
    # of the circuit and not of the units. Defining VA and var as `volt * ampere` made
    # them silent aliases of the watt and of each other, so 100 VA converted to 100 W and
    # 100 var without complaint. Separate dimensions are what makes the distinction the
    # comment above them claimed survive a round trip.
    "VA = [apparent_power]",
    "var = [reactive_power]",
    # Index units with no physical dimension of their own. Each is given its own
    # dimension so that a turbidity cannot be compared against a pH, and so that the two
    # turbidity standards cannot be interconverted: NTU is measured with a white-light
    # instrument and FNU with near-infrared, and the services publish both. This is the
    # same class of error the datum registry prevents one level up.
    "NTU = [turbidity]",
    "FNU = [turbidity_fnu]",
    "pH_unit = [acidity]",
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

# Agency codes for the same idea. Extend the mapping for a service not listed here;
# nothing else in the library assumes a particular publisher.
QUALITY_ALIASES: dict[str, str] = {
    # USGS review-status codes
    "A": "approved",
    "R": "reviewed",
    "e": "estimated",
    "E": "estimated",
    "P": "provisional",
    "p": "provisional",
    # USGS condition codes. These describe the state of the measurement rather than its
    # review status, and each marks a reading the rating does not properly represent.
    # Ice-affected and backwater-affected discharge can be wrong by a large margin while
    # still looking entirely plausible, so they are graded, not dropped.
    "Ice": "unverified",   # ice-affected
    "Bkw": "unverified",   # backwater-affected
    "Eqp": "unverified",   # equipment malfunction
    "Fld": "unverified",   # flood damage
    "Dis": "unverified",   # record discontinued
    "Mnt": "unverified",   # maintenance in progress
    "***": "unverified",   # temporarily unavailable
    "Rat": "estimated",    # rating under development or revision
    "Ssn": "estimated",    # monitored seasonally
    "Dry": "estimated",    # channel dry
    "ZFl": "estimated",    # zero flow
    # UK Environment Agency
    "Good": "approved",
    "Unchecked": "provisional",
    "Estimated": "estimated",
    "Suspect": "unverified",
    "Missing": "unverified",
}


def normalize_unit_text(text: str) -> str:
    """Rewrite the spellings that occur in prose and in service payloads into pint's.

    ``m3/s`` and ``ft3/s`` are how NWIS writes its unit codes and how models write units
    in prose, and pint understands neither. The answer audit normalised them while the
    tool boundary did not, so the same string was valid in an answer and rejected as an
    argument — penalising a model for stating its unit in the form the service published.
    """
    out = text.replace("²", "2").replace("³", "3").replace("^", "**")
    # A hyphen joins two units into a product: acre-ft, ft-lb.
    out = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "*", out)
    # A digit directly after a letter is an exponent, unless it follows a number (so the
    # exponent in "1e3" is left alone).
    return re.sub(r"(?<![*\d])([A-Za-zµ°])(\d)", r"\1**\2", out)


def normalize_quality(flag: str | None) -> str | None:
    """A canonical quality name for a publisher's flag.

    Single-letter agency codes are case-significant and matched exactly, since USGS uses
    ``e`` and ``E`` for different record classes elsewhere in its vocabulary. Word forms
    are matched case-insensitively, because publishers are inconsistent about them.
    """
    if flag is None:
        return None
    if flag in QUALITY_ALIASES:
        return QUALITY_ALIASES[flag]
    text = str(flag).strip()
    if text.lower() in QUALITY_RANK:
        return text.lower()
    words = {k.lower(): v for k, v in QUALITY_ALIASES.items() if len(k) > 1}
    if text.lower() in words:
        return words[text.lower()]
    raise QualityViolation(
        f"unknown quality flag {flag!r}; known grades are {sorted(QUALITY_RANK)} and "
        f"known publisher codes are {sorted(QUALITY_ALIASES)}. Register a flag for a "
        f"publisher not listed by adding it to "
        f"quantity_guard.registry.QUALITY_ALIASES"
    )


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

# National vertical datums in common use. Registering a name only makes it referenceable;
# every offset between two of them is location-dependent and must still be given
# explicitly. Add any datum not listed here with ``datums.register``.
for _name, _description in [
    # North America
    ("NAVD88", "North American Vertical Datum of 1988"),
    ("NGVD29", "National Geodetic Vertical Datum of 1929"),
    ("CGVD2013", "Canadian Geodetic Vertical Datum of 2013"),
    ("CGVD28", "Canadian Geodetic Vertical Datum of 1928"),
    # Britain and Ireland
    ("ODN", "Ordnance Datum Newlyn, mainland Great Britain"),
    ("BELFAST", "Belfast Ordnance Datum, Northern Ireland"),
    ("MALIN", "Malin Head datum, Ireland"),
    # Continental Europe
    ("NAP", "Normaal Amsterdams Peil, Netherlands"),
    ("EVRF2019", "European Vertical Reference Frame 2019"),
    ("EVRF2000", "European Vertical Reference Frame 2000"),
    ("DHHN2016", "Deutsches Haupthoehennetz 2016, Germany"),
    ("NGF-IGN69", "Nivellement General de la France, IGN69"),
    # Elsewhere
    ("AHD", "Australian Height Datum"),
    ("NZVD2016", "New Zealand Vertical Datum 2016"),
    # Global and tidal
    ("EGM2008", "Earth Gravitational Model 2008 geoid"),
    ("EGM96", "Earth Gravitational Model 1996 geoid"),
    ("MSL", "Mean sea level, station-dependent"),
    ("MLLW", "Mean lower low water"),
    ("MHHW", "Mean higher high water"),
    ("LAT", "Lowest astronomical tide, the usual chart datum in Europe"),
    ("CD", "Chart datum, port-specific"),
    ("GAGE", "Local gage or gauge datum, station-specific offset required"),
]:
    datums.register(_name, description=_description)
