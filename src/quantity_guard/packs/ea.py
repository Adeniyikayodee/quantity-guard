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
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..quantity import Q
from ..registry import datums

BASE = "https://environment.data.gov.uk/flood-monitoring"

#: Unit names as the service writes them. ``mASD`` and ``mAOD`` are both metres; what
#: differs is the surface they are measured from, which is why the datum is returned
#: alongside rather than folded into the unit.
#:
#: ``mBDAT`` (metres below datum) is deliberately absent. It measures downward, so
#: mapping it to metres on the station datum would invert the sign of every reading, and
#: this library has no concept of a downward-positive reference. Refusing it is safer
#: than silently mislabelling it; add it only alongside sign handling.
UNIT_NAMES: dict[str, tuple[str, str | None]] = {
    "mASD": ("meter", "station"),
    "mAOD": ("meter", "ODN"),
    "m": ("meter", None),
    "mm": ("millimeter", None),
    "m3/s": ("meter**3/second", None),
    "l/s": ("liter/second", None),
    "Ml/d": ("megaliter/day", None),
    # Further names the live service publishes.
    "mm/hr": ("millimeter/hour", None),
    "m/s": ("meter/second", None),
    "C": ("degC", None),
    "deg": ("degree", None),
    "%": ("percent", None),
    "hPa": ("hectopascal", None),
    "ug/l": ("microgram/liter", None),
    "V": ("volt", None),
    # Housekeeping measures such as battery state are published with no unit at all.
    "---": ("dimensionless", None),
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

    @property
    def age(self) -> timedelta:
        """How long ago the reading was taken."""
        return datetime.now(timezone.utc) - self.observed_at

    def is_stale(self, max_age: timedelta) -> bool:
        return self.age > max_age


def _ref(reference: str) -> str:
    """A station reference as the service spells it in a resource path.

    Agency references are not all bare alphanumerics. The live station list carries
    entries such as ``055003_TG 316``, whose space made an unescaped URL invalid at the
    client. Percent-encoding it is not the answer either: the service substitutes an
    underscore for the space in its own ``@id``, and answers only to that form.

        /id/stations/055003_TG_316    -> 200
        /id/stations/055003_TG%20316  -> 500
        /id/stations/055003_TG+316    -> 404

    Anything else is escaped normally, so an unexpected character fails as a clean 404
    rather than as a client-side ``InvalidURL``.
    """
    return urllib.parse.quote(str(reference).strip().replace(" ", "_"), safe="_")


def _quality(measure: dict, latest: dict) -> str | None:
    """The record grade for a reading, when the service states one.

    The real-time flood-monitoring API publishes no grade: neither a measure nor a
    reading carries one, and ``qualifier`` names the measurement position ("Stage",
    "Downstream Stage") rather than the quality of the record. Quality is therefore
    ``None`` for live EA data, which is a fact about the source rather than a gap here.

    The archived readings do carry Good / Unchecked / Estimated / Suspect / Missing, and
    those words are understood by ``normalize_quality``, so a caller feeding archive rows
    through this pack gets them graded. The key is read from the reading first and the
    measure second, which is where the archive exports put it.

    The previous implementation read ``qualityControl``, a key that exists on neither
    object, so EA quality was silently always ``None`` and the alias table was unreachable.
    """
    for source in (latest, measure):
        flag = source.get("quality")
        if flag:
            return flag
    return None


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
    item = json.loads(fetch(f"{BASE}/id/stations/{_ref(reference)}"))["items"]
    if isinstance(item, list):
        item = item[0]

    datum_name = f"GAUGE:{reference}"
    offset = item.get("datumOffset")
    quantity = None
    # The station's own zero is a real reference frame whether or not its height above
    # Ordnance Datum is published, and most stations do not publish one. Registering the
    # name is what lets a level be *labelled* with the frame it was measured from; the
    # offset is what would additionally let it be *converted* to ODN. Only the second is
    # conditional. Tying both to datumOffset made every station without one unreadable.
    if register and datum_name not in datums.datums:
        datums.register(datum_name, description=f"Local datum for EA station {reference}")
    if offset is not None:
        quantity = Q(float(offset), "meter", datum="ODN")
        if register and not datums.can_convert(datum_name, "ODN"):
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
             datum_name: str | None = None,
             max_age: timedelta | None = None) -> list[Reading]:
    """Latest reading for each measure at a station.

    ``datum_name`` attaches a registered station datum to ``mASD`` levels. Call
    ``station`` first to obtain one; without it a stage is returned with no datum, which
    is honest but leaves it unusable against an absolute elevation.

    ``max_age`` drops readings older than the given age, with a warning naming what was
    dropped. It defaults to ``None``, which returns whatever the service last held for
    each measure — and a station with a failed sensor keeps serving that sensor's last
    good value indefinitely, with nothing in the response marking it as old.
    """
    fetch = fetch or _http
    payload = json.loads(fetch(f"{BASE}/id/stations/{_ref(reference)}/measures"))
    items = payload["items"]
    items = items if isinstance(items, list) else [items]

    out: list[Reading] = []
    for measure in items:
        latest = measure.get("latestReading")
        if not isinstance(latest, dict) or latest.get("value") is None:
            continue
        # One unmapped measure must not cost the caller the whole station; see the same
        # reasoning in the USGS pack.
        try:
            unit, surface = unit_for(measure.get("unitName", ""))
        except ValueError as exc:
            warnings.warn(f"skipping EA measure at {reference}: {exc}", stacklevel=2)
            continue
        datum = datum_name if surface == "station" else surface
        entry = Reading(
            value=Q(
                float(latest["value"]), unit,
                datum=datum,
                quality=_quality(measure, latest),
                source=f"ea:{reference}",
            ),
            # The service stamps every reading in UTC.
            observed_at=datetime.fromisoformat(latest["dateTime"].replace("Z", "+00:00")),
            measure=measure.get("parameterName") or measure.get("parameter") or "",
            qualifier=measure.get("qualifier") or "",
        )
        if max_age is not None and entry.is_stale(max_age):
            warnings.warn(
                f"dropping EA measure {entry.measure or measure.get('notation')} at "
                f"{reference}: last reading is {entry.age.days} days old "
                f"({entry.observed_at.date()}), older than the {max_age} requested",
                stacklevel=2,
            )
            continue
        out.append(entry)
    return out


def reading(reference: str, fetch: Fetch | None = None,
            max_age: timedelta | None = None) -> tuple[Station, list[Reading]]:
    """A station record and its latest readings, with the datum already wired up.

    ``max_age`` is passed through to :func:`readings`.
    """
    record = station(reference, fetch=fetch)
    return record, readings(reference, fetch=fetch, datum_name=record.datum_name,
                            max_age=max_age)
