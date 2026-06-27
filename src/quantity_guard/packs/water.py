"""Hydrology pack.

Reusable specifications for the quantities that appear in surface water work, together
with the station datum handling that stage measurements require.

Gage height is reported against a local station datum, whereas flood stage and structure
elevations are usually published against NAVD88. The two are both lengths, so nothing in
ordinary dimensional analysis prevents them being differenced, and the resulting error is
silent. Registering a station makes that comparison either correct or refused.
"""

from __future__ import annotations

from ..quantity import Q
from ..registry import datums
from ..spec import Spec

# Common measurement specifications ------------------------------------------------------

DISCHARGE = Spec(
    unit="m**3/s",
    quality=None,
    description="Volumetric flow rate. USGS publishes this as cfs.",
)

GAGE_HEIGHT = Spec(
    unit="ft",
    datum="GAGE",
    description="Stage above the local station datum, as published in USGS parameter 00065.",
)

ELEVATION = Spec(
    unit="ft",
    datum="NAVD88",
    description="Absolute elevation.",
)

WATER_TEMPERATURE = Spec(unit="degC", description="Water temperature.")

PRECIPITATION = Spec(unit="mm", description="Depth of precipitation over the interval.")

DRAINAGE_AREA = Spec(unit="km**2", description="Contributing drainage area.")

OBSERVED_AT = Spec(
    tz="America/Chicago",
    description="Observation time. USGS publishes in local standard time, not UTC.",
)


def register_station(station_id: str, gage_datum_elevation: Q) -> str:
    """Register a gage's local datum and its elevation on NAVD88.

    ``gage_datum_elevation`` is the elevation of the station's zero point, as published in
    the USGS site record. Returns the datum name to use in specs for that station.

    >>> name = register_station("07374000", Q(1.5, "ft", datum="NAVD88"))
    >>> name
    'GAGE:07374000'
    """
    name = f"GAGE:{station_id}"
    datums.register(
        name,
        description=f"Local datum for USGS station {station_id}",
    )
    datums.register_offset(name, "NAVD88", gage_datum_elevation.to("meter").magnitude)
    return name


def station_spec(station_id: str, unit: str = "ft") -> Spec:
    """Gage height spec bound to a registered station's datum."""
    return Spec(
        unit=unit,
        datum=f"GAGE:{station_id}",
        description=f"Stage at USGS station {station_id}, on its local datum.",
    )
