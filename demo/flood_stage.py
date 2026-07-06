"""Three failure modes from agent transcripts, run against unguarded and guarded tools.

The scenario is a flood assessment at a USGS streamgage. Gage height is published against
the station's local datum, flood stage against NAVD88, and discharge in cubic feet per
second, which is the combination that produces most quantitative errors in water work.

The agent's tool calls are scripted rather than generated, so the demo runs offline and
reproduces the same three errors every time. Each is a pattern reported in the literature
on agent numeric reliability: a magnitude carried between tools without its unit, a
comparison of two elevations on different vertical references, and a figure stated in the
answer that no tool produced.

Run with:  python demo/flood_stage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quantity_guard import GuardViolation, Q, quantity_tool, session  # noqa: E402
from quantity_guard.packs.water import register_station  # noqa: E402

STATION = "07374000"  # Mississippi River at Baton Rouge, LA
GAGE_DATUM = register_station(STATION, Q(1.5, "ft", datum="NAVD88"))

RULE = "=" * 78


# Tools -----------------------------------------------------------------------------------


@quantity_tool(returns={"unit": "cfs"})
def read_discharge(station: str):
    """Latest observed discharge at a streamgage, as published by USGS in cfs."""
    return Q(1250.0, "cfs", quality="P", source=f"usgs:{station}")


@quantity_tool(returns={"unit": "ft", "datum": GAGE_DATUM})
def read_gage_height(station: str):
    """Latest observed stage, referenced to the station's local datum."""
    return Q(12.4, "ft", datum=GAGE_DATUM, quality="P", source=f"usgs:{station}")


@quantity_tool(returns={"unit": "ft", "datum": "NAVD88"})
def read_flood_stage(station: str):
    """Published flood stage for the site, referenced to NAVD88."""
    return Q(31.0, "ft", datum="NAVD88", quality="A", source="nws:ahps")


@quantity_tool(
    params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
    returns={"unit": "mm/day"},
)
def runoff_depth(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return discharge / area


@quantity_tool(
    params={
        "stage": {"unit": "ft", "datum": "NAVD88"},
        "flood_stage": {"unit": "ft", "datum": "NAVD88"},
    },
    returns={"unit": "ft"},
)
def freeboard(stage, flood_stage):
    """Vertical margin between the current water surface and flood stage."""
    return flood_stage - stage


# Unguarded equivalents, written the way the same tools are usually written ---------------


def raw_runoff_depth(discharge_value, area_value):
    """Discharge in m3/s, area in km2, returning mm/day."""
    return discharge_value / (area_value * 1e6) * 1000 * 86400
