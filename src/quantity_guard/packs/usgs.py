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
