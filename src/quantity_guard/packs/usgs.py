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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from ..quantity import Q
from ..registry import datums

IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

#: Unit codes as the service writes them, in the spelling pint understands.
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
}

Fetch = Callable[[str], str]


def _http(url: str) -> str:  # pragma: no cover - exercised only against the live service
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


@dataclass
class Observation:
    """One reading, with the time it was taken."""

    value: Q
    observed_at: datetime
    parameter: str
    name: str


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


def unit_for(code: str) -> str:
    """A pint unit for a service unit code."""
    cleaned = code.strip()
    if cleaned in UNIT_CODES:
        return UNIT_CODES[cleaned]
    raise ValueError(
        f"unmapped USGS unit code {code!r}; add it to quantity_guard.packs.usgs.UNIT_CODES"
    )


def _quality(qualifiers: Iterable[str]) -> str | None:
    """The weakest qualifier on a reading, as a quality flag."""
    for code in ("P", "e", "A"):
        if code in set(qualifiers):
            return code
    return None


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
    altitude_datum = field.get("alt_datum_cd", "").strip() or "NAVD88"
    area = field.get("drain_area_va", "").strip()
    zone = field.get("tz_cd", "").strip() or "UTC"
    datum_name = f"GAGE:{number}"

    gage_datum = None
    if altitude:
        if altitude_datum not in datums.datums:
            datums.register(altitude_datum, description=f"Reported by USGS for {number}")
        gage_datum = Q(float(altitude), "ft", datum=altitude_datum)
        if register:
            if datum_name not in datums.datums:
                datums.register(datum_name, description=f"Local datum for USGS {number}")
            if not datums.can_convert(datum_name, altitude_datum):
                datums.register_offset(datum_name, altitude_datum, gage_datum)

    return Site(
        number=number,
        name=field.get("station_nm", "").strip(),
        datum_name=datum_name,
        gage_datum=gage_datum,
        drainage_area=Q(float(area), "mile**2").to("km**2") if area else None,
        timezone=zone,
        horizontal_crs=field.get("dec_coord_datum_cd", "").strip() or None,
    )


def instantaneous(number: str, parameters: Iterable[str] = ("00060", "00065"),
                  fetch: Fetch | None = None,
                  datum_name: str | None = None) -> dict[str, Observation]:
    """Latest instantaneous values, as quantities keyed by parameter code.

    ``datum_name`` attaches a registered station datum to stage readings. Call ``site``
    first to obtain and register one; without it a gage height is returned with no datum,
    which is honest but leaves it unusable against an absolute elevation.
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
        if point["value"] in ("", "-999999"):
            continue

        quantity = Q(
            float(point["value"]),
            unit_for(variable["unit"]["unitCode"]),
            quality=_quality(point.get("qualifiers", [])),
            source=f"usgs:{number}:{code}",
            # Stage is measured from the station's own zero, not from sea level.
            datum=datum_name if code == "00065" else None,
        )
        readings[code] = Observation(
            value=quantity,
            # The service stamps every reading with its own offset, so the timezone is
            # read from the data rather than assumed for the region.
            observed_at=datetime.fromisoformat(point["dateTime"]),
            parameter=code,
            name=variable["variableName"].split(",")[0],
        )
    return readings


def reading(number: str, fetch: Fetch | None = None) -> tuple[Site, dict[str, Observation]]:
    """A site record and its latest values, with the datum already wired up."""
    record = site(number, fetch=fetch)
    values = instantaneous(number, fetch=fetch, datum_name=record.datum_name)
    return record, values
