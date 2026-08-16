"""Retrieval from USGS Water Services, with the metadata kept.

The service already publishes everything needed to make a reading self-describing: a unit
code on each variable, a qualifier marking the record provisional or approved, an explicit
UTC offset on every timestamp, and a site record giving the gage datum and the reference
it is measured from. Clients typically parse the number and drop the rest.

This returns quantities instead. Gage height comes back on the station's own datum, which
is registered from the site record, so differencing it against an elevation on NAVD88 is
refused rather than silently wrong.

Network access is through a replaceable ``fetch``, so tests run against recorded
responses.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..quantity import Q
from ..registry import QUALITY_ALIASES, datums, worst_quality

IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

#: Unit codes as the service writes them, in the spelling pint understands. Matched
#: case-insensitively, since NWIS publishes "deg C" while the natural key here is
#: lowercase, and one casing failing while the other works is not a distinction worth
#: carrying.
UNIT_CODES = {
    "ft3/s": "foot**3/second",
    "ft": "foot",
    "in": "inch",
    "deg c": "degC",
    "deg f": "degF",
    "mg/l": "milligram/liter",
    "uS/cm @25C": "microsiemens/centimetre",
    "ft3/s/mi2": "foot**3/second/mile**2",
    "mi2": "mile**2",
    # Further codes the live service publishes.
    "m3/s": "meter**3/second",
    "m": "meter",
    "mm": "millimeter",
    "ft/sec": "foot/second",
    "ft/s": "foot/second",
    "ac-ft": "acre*foot",
    "mgd": "mgd",
    "ug/l": "microgram/liter",
    "in/hr": "inch/hour",
    "mph": "mile/hour",
    "tons/day": "ton/day",
    "%": "percent",
    # Index parameters. Each carries its own dimension, so a pH cannot be differenced
    # against a turbidity and the two turbidity standards cannot be interconverted.
    "std units": "pH_unit",
    "NTU": "NTU",
    "FNU": "FNU",
}

Fetch = Callable[[str], str]


def _http(url: str) -> str:  # pragma: no cover - exercised only against the live service
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


@dataclass
class Observation:
    """One reading, with the time it was taken.

    The service returns the last value it holds for a parameter regardless of how old
    that is, so "latest" and "current" are not the same thing. At a single site the
    discharge can be minutes old while the temperature is years old, from one call.
    """

    value: Q
    observed_at: datetime
    parameter: str
    name: str

    @property
    def age(self) -> timedelta:
        """How long ago the reading was taken."""
        return datetime.now(timezone.utc) - self.observed_at

    def is_stale(self, max_age: timedelta) -> bool:
        return self.age > max_age


@dataclass
class Site:
    """The parts of a site record that change how a reading must be handled."""

    number: str
    name: str
    datum_name: str
    gage_datum: Q | None
    drainage_area: Q | None
    timezone: str
    horizontal_crs: str | None
    #: Published accuracy of ``gage_datum`` (``alt_acy_va``), in feet. Commonly 0.01 ft
    #: on a modern survey and 10 or 20 ft on an older one, which bounds how much any
    #: elevation derived through the gage offset can be trusted.
    gage_datum_accuracy: Q | None = None


def unit_for(code: str) -> str:
    """A pint unit for a service unit code.

    The exact spelling wins, so a runtime addition to ``UNIT_CODES`` is honoured; failing
    that the comparison is case-insensitive. NWIS publishes "deg C" and the table is
    keyed on "deg c", which previously made water temperature unretrievable.
    """
    cleaned = code.strip()
    if cleaned in UNIT_CODES:
        return UNIT_CODES[cleaned]
    folded = cleaned.casefold()
    for published, unit in UNIT_CODES.items():
        if published.casefold() == folded:
            return unit
    raise ValueError(
        f"unmapped USGS unit code {code!r}; add it to quantity_guard.packs.usgs.UNIT_CODES"
    )


def _quality(qualifiers: Iterable[str]) -> str | None:
    """The weakest qualifier on a reading, as a quality flag.

    Review-status codes (A, R, P, e) and condition codes (Ice, Bkw, Eqp) are both read,
    and the weakest of everything present wins. A reading marked ``["A", "Ice"]`` is
    approved record of an ice-affected measurement, which is not approved-quality data.
    Unrecognised qualifiers are ignored rather than raising, since NWIS carries footnote
    codes that say nothing about record quality.
    """
    flags = [QUALITY_ALIASES[code] for code in set(qualifiers) if code in QUALITY_ALIASES]
    return worst_quality(*flags)


#: The sentinel NWIS uses when a value is absent, for series that omit ``noDataValue``.
DEFAULT_NO_DATA = -999999.0


def _is_missing(raw: Any, no_data: Any = None) -> bool:
    """Whether a published point represents no measurement.

    Compared numerically against the sentinel the service states for the variable, so
    every spelling of it is caught. A non-numeric value is missing too: NWIS writes an
    empty string, and occasionally other placeholder text, for a gap in the record.
    """
    text = str(raw).strip()
    if not text:
        return True
    try:
        value = float(text)
    except ValueError:
        return True
    sentinel = DEFAULT_NO_DATA if no_data is None else float(no_data)
    return value == sentinel


def site(number: str, fetch: Fetch | None = None, register: bool = True) -> Site:
    """Read a site record, and register its datum so stages carry a real reference."""
    fetch = fetch or _http
    query = urllib.parse.urlencode(
        {"format": "rdb", "sites": number, "siteOutput": "expanded"})
    rows = [line for line in fetch(f"{SITE_URL}?{query}").splitlines()
            if line and not line.startswith("#")]
    header, record = rows[0].split("\t"), rows[2].split("\t")
    field = dict(zip(header, record))

    altitude = field.get("alt_va", "").strip()
    # Never defaulted. The library refuses to guess an offset between two datums because
    # it varies with location; guessing which datum a published altitude is measured
    # from is the same error one step earlier, and worse, because every stage converted
    # through the resulting offset silently inherits the assumption. Many older NWIS
    # sites are on NGVD29, and the difference is about the size of the freeboard error
    # the datum subsystem exists to prevent.
    altitude_datum = field.get("alt_datum_cd", "").strip()
    accuracy = field.get("alt_acy_va", "").strip()
    area = field.get("drain_area_va", "").strip()
    zone = field.get("tz_cd", "").strip() or "UTC"
    datum_name = f"GAGE:{number}"

    # The station's own zero is a reference frame whether or not its height above a
    # national datum is published, so the name is registered either way; only the offset
    # is conditional. See the same reasoning in the Environment Agency pack.
    if register and datum_name not in datums.datums:
        datums.register(datum_name, description=f"Local datum for USGS {number}")

    gage_datum = None
    if altitude and altitude_datum:
        if altitude_datum not in datums.datums:
            datums.register(altitude_datum, description=f"Reported by USGS for {number}")
        gage_datum = Q(float(altitude), "ft", datum=altitude_datum)
        if register and not datums.can_convert(datum_name, altitude_datum):
            datums.register_offset(datum_name, altitude_datum, gage_datum)
    elif altitude:
        warnings.warn(
            f"USGS site {number} publishes an altitude of {altitude} ft but no "
            f"alt_datum_cd, so the datum it is measured from is unknown and no offset "
            f"from {datum_name} has been registered; stages will be labelled but not "
            f"convertible to an absolute elevation",
            stacklevel=2,
        )

    return Site(
        number=number,
        name=field.get("station_nm", "").strip(),
        datum_name=datum_name,
        gage_datum=gage_datum,
        drainage_area=Q(float(area), "mile**2").to("km**2") if area else None,
        timezone=zone,
        horizontal_crs=field.get("dec_coord_datum_cd", "").strip() or None,
        gage_datum_accuracy=Q(float(accuracy), "ft") if accuracy else None,
    )


def instantaneous(number: str, parameters: Iterable[str] = ("00060", "00065"),
                  fetch: Fetch | None = None,
                  datum_name: str | None = None,
                  max_age: timedelta | None = None) -> dict[str, Observation]:
    """Latest instantaneous values, as quantities keyed by parameter code.

    ``datum_name`` attaches a registered station datum to stage readings. Call ``site``
    first to obtain and register one; without it a gage height is returned with no datum,
    which is honest but leaves it unusable against an absolute elevation.

    ``max_age`` drops readings older than the given age, with a warning naming what was
    dropped. It defaults to ``None``, which returns whatever the service last held. That
    default keeps the function faithful to the endpoint, but it is rarely what a caller
    wants: the service serves the last value for each parameter independently, so one
    response can mix a reading minutes old with one years old. Observed live at
    01646500, in a single call:

        00060 Streamflow            3010 ft3/s   2026-08-15   <- current
        00065 Gage height           3.03 ft      2026-08-15   <- current
        00010 Temperature           24.3 degC    2019-10-01   <- seven years old
        63680 Turbidity             6.2 FNU      2019-05-27   <- seven years old

    Nothing downstream can recover the distinction, because the ledger records the
    magnitude and its unit, not when it was measured. Pass ``max_age`` for any use where
    the reading standing for "now" matters, which for flood work is all of them.
    """
    fetch = fetch or _http
    query = urllib.parse.urlencode({
        "format": "json", "sites": number,
        "parameterCd": ",".join(parameters), "siteStatus": "all",
    })
    payload = json.loads(fetch(f"{IV_URL}?{query}"))

    readings: dict[str, Observation] = {}
    for series in payload["value"]["timeSeries"]:
        variable = series["variable"]
        code = variable["variableCode"][0]["value"]
        points = series["values"][0]["value"]
        if not points:
            continue
        point = points[-1]
        # The service publishes its own sentinel per variable, so read it rather than
        # hard-coding one spelling. The previous literal string comparison against
        # "-999999" let "-999999.0" and "-999999.00" through as a real measurement:
        # dimensionally valid, carrying a genuine quality flag, and passing every guard.
        if _is_missing(point["value"], variable.get("noDataValue")):
            continue

        # One unmapped parameter must not cost the caller the whole station. A site
        # commonly serves discharge and stage alongside a water-quality sensor whose
        # unit is not in the table, and losing the flood-relevant values to that is a
        # worse outcome than omitting the sensor, provided the omission is visible.
        try:
            units = unit_for(variable["unit"]["unitCode"])
        except ValueError as exc:
            warnings.warn(f"skipping USGS parameter {code}: {exc}", stacklevel=2)
            continue

        quantity = Q(
            float(point["value"]),
            units,
            quality=_quality(point.get("qualifiers", [])),
            source=f"usgs:{number}:{code}",
            # Stage is measured from the station's own zero, not from sea level.
            datum=datum_name if code == "00065" else None,
        )
        observation = Observation(
            value=quantity,
            # The service stamps every reading with its own offset, so the timezone is
            # read from the data rather than assumed for the region.
            observed_at=datetime.fromisoformat(point["dateTime"]),
            parameter=code,
            name=variable["variableName"].split(",")[0],
        )
        if max_age is not None and observation.is_stale(max_age):
            warnings.warn(
                f"dropping USGS parameter {code} at {number}: last reading is "
                f"{observation.age.days} days old "
                f"({observation.observed_at.date()}), older than the {max_age} requested",
                stacklevel=2,
            )
            continue
        readings[code] = observation
    return readings


def reading(number: str, fetch: Fetch | None = None,
            max_age: timedelta | None = None) -> tuple[Site, dict[str, Observation]]:
    """A site record and its latest values, with the datum already wired up.

    ``max_age`` is passed through to :func:`instantaneous`; see the note there on why
    "latest" and "current" are not the same thing at a USGS site.
    """
    record = site(number, fetch=fetch)
    values = instantaneous(number, fetch=fetch, datum_name=record.datum_name,
                           max_age=max_age)
    return record, values
