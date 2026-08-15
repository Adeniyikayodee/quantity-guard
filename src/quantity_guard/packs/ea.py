"""Retrieval from the UK Environment Agency flood-monitoring service.

The same hazard as the USGS pack, in a different vocabulary. Levels are published either
as ``mASD``, metres above the station's own datum, or ``mAOD``, metres above Ordnance
Datum Newlyn. Differencing one against the other is dimensionally valid and physically
wrong, and the station record carries the offset needed to relate them.

The service is open and needs no key. Network access is through a replaceable ``fetch``,
so tests run against recorded responses.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..quantity import Q
from ..registry import datums

BASE = "https://environment.data.gov.uk/flood-monitoring"

#: Unit names as the service writes them. ``mASD`` and ``mAOD`` are both metres; what
#: differs is the surface they are measured from, which is why the datum is returned
#: alongside rather than folded into the unit.
UNIT_NAMES: dict[str, tuple[str, str | None]] = {
    "mASD": ("meter", "station"),
    "mAOD": ("meter", "ODN"),
    "m": ("meter", None),
    "mm": ("millimeter", None),
    "m3/s": ("meter**3/second", None),
    "l/s": ("liter/second", None),
    "Ml/d": ("megaliter/day", None),
}

Fetch = Callable[[str], str]


def _http(url: str) -> str:  # pragma: no cover - exercised only against the live service
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


@dataclass
class Station:
    reference: str
    name: str
    datum_name: str
    #: Height of the station's zero above Ordnance Datum Newlyn, when published.
    datum_offset: Q | None
    latitude: float | None
    longitude: float | None


@dataclass
class Reading:
    value: Q
    observed_at: datetime
    measure: str
    qualifier: str


def unit_for(name: str) -> tuple[str, str | None]:
    """A pint unit and the datum it is measured from, for a service unit name."""
    if name not in UNIT_NAMES:
        raise ValueError(
            f"unmapped Environment Agency unit {name!r}; add it to "
            f"quantity_guard.packs.ea.UNIT_NAMES"
        )
    return UNIT_NAMES[name]


def station(reference: str, fetch: Fetch | None = None, register: bool = True) -> Station:
    """Read a station record, registering its datum when the offset is published."""
    fetch = fetch or _http
    item = json.loads(fetch(f"{BASE}/id/stations/{reference}"))["items"]
    if isinstance(item, list):
        item = item[0]

    datum_name = f"GAUGE:{reference}"
    offset = item.get("datumOffset")
    quantity = None
    if offset is not None:
        quantity = Q(float(offset), "meter", datum="ODN")
        if register:
            if datum_name not in datums.datums:
                datums.register(datum_name,
                                description=f"Local datum for EA station {reference}")
            if not datums.can_convert(datum_name, "ODN"):
                datums.register_offset(datum_name, "ODN", quantity)

    return Station(
        reference=reference,
        name=item.get("label") if isinstance(item.get("label"), str) else reference,
        datum_name=datum_name,
        datum_offset=quantity,
        latitude=item.get("lat"),
        longitude=item.get("long"),
    )


def readings(reference: str, fetch: Fetch | None = None,
             datum_name: str | None = None) -> list[Reading]:
    """Latest reading for each measure at a station.

    ``datum_name`` attaches a registered station datum to ``mASD`` levels. Call
    ``station`` first to obtain one; without it a stage is returned with no datum, which
    is honest but leaves it unusable against an absolute elevation.
    """
    fetch = fetch or _http
    payload = json.loads(fetch(f"{BASE}/id/stations/{reference}/measures"))
    items = payload["items"]
    items = items if isinstance(items, list) else [items]

    out: list[Reading] = []
    for measure in items:
        latest = measure.get("latestReading")
        if not isinstance(latest, dict) or latest.get("value") is None:
            continue
        unit, surface = unit_for(measure.get("unitName", ""))
        datum = datum_name if surface == "station" else surface
        out.append(Reading(
            value=Q(
                float(latest["value"]), unit,
                datum=datum,
                quality=measure.get("qualityControl"),
                source=f"ea:{reference}",
            ),
            # The service stamps every reading in UTC.
            observed_at=datetime.fromisoformat(latest["dateTime"].replace("Z", "+00:00")),
            measure=measure.get("parameterName") or measure.get("parameter") or "",
            qualifier=measure.get("qualifier") or "",
        ))
    return out


def reading(reference: str, fetch: Fetch | None = None) -> tuple[Station, list[Reading]]:
    """A station record and its latest readings, with the datum already wired up."""
    record = station(reference, fetch=fetch)
    return record, readings(reference, fetch=fetch, datum_name=record.datum_name)
