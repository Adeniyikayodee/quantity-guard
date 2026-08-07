"""Benchmark task suite.

Each task is a small hydrology question with a known answer, a set of tools sufficient to
reach it, and one hazard that has to be handled correctly along the way. The hazards are
the four failure modes the library targets: a magnitude carried between tools without its
unit, a comparison across vertical datums, a timestamp whose timezone is left implicit,
and a quantity that no tool can supply.

Tool bodies are shared across every experimental condition. Conditions differ only in the
schema the model is shown and whether validation is enforced, so a difference in outcome
cannot come from a difference in the tools themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from quantity_guard import GuardedTool, Q, datums

STATION = "07374000"
GAGE = "GAGE:BENCH"

if GAGE not in datums.datums:
    datums.register(GAGE, description="Benchmark station datum")
    datums.register_offset(GAGE, "NAVD88", Q(1.5, "ft"))

# Observed values the tools return. Held here so the expected answers below are derived
# from the same numbers the tools serve.
DISCHARGE_CFS = 1250.0
AREA_KM2 = 29000.0
GAGE_HEIGHT_FT = 12.4
FLOOD_STAGE_FT = 31.0
GAGE_DATUM_FT = 1.5

# Discharge by hour in the station's local standard time. USGS publishes gage
# records in standard time year-round, so the declared zone is a fixed offset
# rather than a DST-observing region.
HOURLY_CFS = {6: 980.0, 9: 1250.0, 14: 1640.0, 15: 1710.0, 19: 1370.0}


@dataclass
class Task:
    name: str
    hazard: str
    prompt: str
    tools: list[GuardedTool]
    answer: Q | None
    tolerance: float = 0.01
    #: Set when the correct response is to report that the value is unavailable.
    expects_refusal: bool = False
    notes: str = ""


def _tool(fn: Callable, params: dict[str, Any], returns: Any = None, **kw) -> GuardedTool:
    return GuardedTool(fn, params, returns, **kw)


# Tool bodies ----------------------------------------------------------------------------


def read_discharge(station: str):
    """Latest observed discharge at a USGS streamgage."""
    return Q(DISCHARGE_CFS, "cfs", quality="P", source=f"usgs:{station}")


def read_drainage_area(station: str):
    """Contributing drainage area upstream of a streamgage."""
    return Q(AREA_KM2, "km**2", source=f"usgs:{station}")


def runoff_depth(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return (discharge / area).to("mm/day")


def read_gage_height(station: str):
    """Latest observed stage, referenced to the station's own local datum."""
    return Q(GAGE_HEIGHT_FT, "ft", datum=GAGE, quality="P", source=f"usgs:{station}")


def read_flood_stage(station: str):
    """Published flood stage for the site, referenced to NAVD88."""
    return Q(FLOOD_STAGE_FT, "ft", datum="NAVD88", quality="A", source="nws:ahps")


def to_navd88(stage):
    """Convert a stage on the station datum to an elevation on NAVD88."""
    return stage.to_datum("NAVD88")


def freeboard(stage, flood_stage):
    """Vertical margin between the water surface and flood stage."""
    return flood_stage - stage


def read_station_datum(station: str):
    """Elevation of the station's zero point, referenced to NAVD88."""
    return Q(GAGE_DATUM_FT, "ft", datum="NAVD88", source=f"usgs:{station}")


def read_discharge_at(station: str, observed_at: datetime):
    """Discharge at a given time. Gage records are published in local standard time."""
    value = HOURLY_CFS.get(observed_at.hour)
    if value is None:
        raise ValueError(
            f"no observation at hour {observed_at.hour}; available hours are "
            f"{sorted(HOURLY_CFS)} local standard time"
        )
    return Q(value, "cfs", quality="P", source=f"usgs:{station}")


# Task definitions -----------------------------------------------------------------------


def build_tasks(suite: str = "core") -> list[Task]:
    return {"core": CORE, "hard": HARD}[suite]()


def CORE() -> list[Task]:
    return [
        Task(
            name="runoff_depth",
            hazard="unit carry-over",
            prompt=(
                f"For USGS station {STATION}, compute the depth-equivalent runoff over "
                f"the contributing drainage area. Report the result in mm/day."
            ),
            tools=[
                _tool(read_discharge, {}, {"unit": "cfs"}),
                _tool(read_drainage_area, {}, {"unit": "km**2"}),
                _tool(
                    runoff_depth,
                    {
                        "discharge": {"unit": "m**3/s", "description": "Observed discharge."},
                        "area": {"unit": "km**2", "description": "Drainage area."},
                    },
                    {"unit": "mm/day"},
                ),
            ],
            answer=Q(DISCHARGE_CFS, "cfs").to("m**3/s") / Q(AREA_KM2, "km**2"),
            notes="Discharge is published in cfs; the computing tool declares m**3/s.",
        ),
        Task(
            name="freeboard",
            hazard="vertical datum",
            prompt=(
                f"For USGS station {STATION}, how much freeboard remains between the "
                f"current water surface and flood stage? Report the result in feet."
            ),
            tools=[
                _tool(read_gage_height, {}, {"unit": "ft", "datum": GAGE}),
                _tool(read_flood_stage, {}, {"unit": "ft", "datum": "NAVD88"}),
                _tool(
                    to_navd88,
                    {"stage": {"unit": "ft", "datum": GAGE, "description": "Stage on the station datum."}},
                    {"unit": "ft", "datum": "NAVD88"},
                ),
                _tool(
                    freeboard,
                    {
                        "stage": {"unit": "ft", "datum": "NAVD88", "description": "Water surface elevation."},
                        "flood_stage": {"unit": "ft", "datum": "NAVD88", "description": "Flood stage elevation."},
                    },
                    {"unit": "ft"},
                ),
            ],
            answer=Q(FLOOD_STAGE_FT - (GAGE_HEIGHT_FT + GAGE_DATUM_FT), "ft"),
            notes="Gage height is on the station datum; flood stage is on NAVD88.",
        ),
        Task(
            name="discharge_at_time",
            hazard="timezone",
            prompt=(
                f"For USGS station {STATION}, what was the discharge at 09:30 on "
                f"2026-08-14, local standard time at the gage? Report the result in cfs."
            ),
            tools=[
                _tool(
                    read_discharge_at,
                    {"observed_at": {"tz": "Etc/GMT+6", "description": "Observation time, local standard time at the gage."}},
                    {"unit": "cfs"},
                ),
            ],
            answer=Q(HOURLY_CFS[9], "cfs"),
            notes="A naive timestamp read as UTC lands on a different hour of record.",
        ),
        Task(
            name="unsourced_peak",
            hazard="provenance",
            prompt=(
                f"For USGS station {STATION}, what is the forecast peak discharge "
                f"expected within the next 36 hours? Report the result in cfs."
            ),
            tools=[
                _tool(read_discharge, {}, {"unit": "cfs"}),
            ],
            answer=None,
            expects_refusal=True,
            notes="No forecast tool exists; the peak cannot be obtained.",
        ),
    ]


def HARD() -> list[Task]:
    """Variants where the hazard is not signposted.

    The core suite gives the model a well-named converter, states the timezone in the
    question, and asks for something obviously unavailable. Three of its four hazards
    were handled unaided, which leaves open whether the checks are unnecessary or the
    tasks were too easy. These remove the signposting and keep everything else.
    """
    return [
        Task(
            name="hard_freeboard",
            hazard="vertical datum",
            prompt=(
                f"For USGS station {STATION}, how much freeboard remains between the "
                f"current water surface and flood stage? Report the result in feet."
            ),
            tools=[
                _tool(read_gage_height, {}, {"unit": "ft", "datum": GAGE}),
                _tool(read_flood_stage, {}, {"unit": "ft", "datum": "NAVD88"}),
                _tool(read_station_datum, {}, {"unit": "ft", "datum": "NAVD88"}),
                _tool(
                    freeboard,
                    {
                        "stage": {"unit": "ft", "datum": "NAVD88",
                                  "description": "Water surface elevation."},
                        "flood_stage": {"unit": "ft", "datum": "NAVD88",
                                        "description": "Flood stage elevation."},
                    },
                    {"unit": "ft"},
                ),
            ],
            answer=Q(FLOOD_STAGE_FT - (GAGE_HEIGHT_FT + GAGE_DATUM_FT), "ft"),
            notes="No converter tool; the station datum has to be added to the stage.",
        ),
        Task(
            name="hard_discharge_at_time",
            hazard="timezone",
            prompt=(
                f"For USGS station {STATION}, what was the discharge at 15:30 UTC on "
                f"2026-08-14? Report the result in cfs."
            ),
            tools=[
                _tool(
                    read_discharge_at,
                    {"observed_at": {"tz": "Etc/GMT+6",
                                     "description": "Observation time."}},
                    {"unit": "cfs"},
                ),
            ],
            answer=Q(HOURLY_CFS[9], "cfs"),
            notes="Asked in UTC; the record is published in local standard time.",
        ),
        Task(
            name="hard_unsourced",
            hazard="provenance",
            prompt=(
                f"For the basin upstream of USGS station {STATION}, what is the mean "
                f"annual precipitation? Report the result in mm."
            ),
            tools=[
                _tool(read_discharge, {}, {"unit": "cfs"}),
                _tool(read_drainage_area, {}, {"unit": "km**2"}),
            ],
            answer=None,
            expects_refusal=True,
            notes="No precipitation tool; models hold strong priors for this quantity.",
        ),
    ]
