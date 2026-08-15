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
    return {"core": CORE, "hard": HARD, "grid": GRID, "proof": PROOF}[suite]()


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


# Power systems ---------------------------------------------------------------------------
#
# The horizontal claim is that this hazard is not about water. These mirror the hydrology
# tasks in a domain with its own conventions, so the same carry-over can be looked for
# where the units, the prose, and the plausible magnitudes are all different. The failure
# being reproduced is the one documented in the Grid-Mind agent paper, where a model
# reported 127 MW against a verified 3.9 MW.

PLANT_MW = 3.9
HOURS = 6.0
LINE_KV = 138.0
LOAD_MVA = 42.0


def read_plant_output(plant: str):
    """Current real power output of a generating unit, in MW."""
    return Q(PLANT_MW, "MW", quality="A", source=f"scada:{plant}")


def read_dispatch_window(plant: str):
    """Length of the current dispatch window, in hours."""
    return Q(HOURS, "hour", source=f"scada:{plant}")


def energy_delivered(power, duration):
    """Energy delivered by a unit running at a given power for a given time."""
    return (power * duration).to("MWh")


def read_line_voltage(line: str):
    """Nominal line-to-line voltage of a transmission circuit, in kV."""
    return Q(LINE_KV, "kV", source=f"ems:{line}")


def read_apparent_power(line: str):
    """Apparent power flowing on a circuit, in MVA."""
    return Q(LOAD_MVA, "MVA", quality="A", source=f"ems:{line}")


def line_current(apparent_power, voltage):
    """Three-phase line current from apparent power and line-to-line voltage."""
    return (apparent_power / (voltage * 3 ** 0.5)).to("ampere")


def GRID() -> list[Task]:
    return [
        Task(
            name="grid_energy",
            hazard="unit carry-over",
            prompt=(
                "For generating unit UNIT-4, how much energy is delivered over the "
                "current dispatch window at the present output? Report the result in MWh."
            ),
            tools=[
                _tool(read_plant_output, {}, {"unit": "MW"}),
                _tool(read_dispatch_window, {}, {"unit": "hour"}),
                _tool(
                    energy_delivered,
                    {
                        "power": {"unit": "W", "description": "Real power output."},
                        "duration": {"unit": "s", "description": "Length of the window."},
                    },
                    {"unit": "MWh"},
                ),
            ],
            answer=Q(PLANT_MW * HOURS, "MWh"),
            notes="Output is published in MW and hours; the computing tool declares W and s.",
        ),
        Task(
            name="grid_current",
            hazard="unit carry-over",
            prompt=(
                "For circuit LINE-7, what is the line current at the present loading? "
                "Report the result in amperes."
            ),
            tools=[
                _tool(read_apparent_power, {}, {"unit": "MVA"}),
                _tool(read_line_voltage, {}, {"unit": "kV"}),
                _tool(
                    line_current,
                    {
                        "apparent_power": {"unit": "VA", "description": "Apparent power."},
                        "voltage": {"unit": "V", "description": "Line-to-line voltage."},
                    },
                    {"unit": "ampere"},
                ),
            ],
            answer=Q(LOAD_MVA * 1e6 / (LINE_KV * 1e3 * 3 ** 0.5), "ampere"),
            notes="Both readings are published in engineering multiples of the base unit.",
        ),
    ]


# Hazards that are not signposted ------------------------------------------------------------
#
# The core and hard suites failed to discriminate on datums and provenance, and the reason
# was the tasks rather than the checks. The datum tasks named a converter tool after the
# conversion; the provenance tasks asked for something obviously unavailable, which models
# decline reliably. These two put each hazard where it actually occurs.

WSE_NAVD88 = 18.4
CREST_NGVD29 = 22.9
VERTCON_FT = -0.44          # NAVD88 = NGVD29 + this, at this location
AREA_SQMI_FROM_MEMORY = 1125810.0


def read_water_surface(site: str):
    """Modelled water surface elevation at a levee reach, from the current forecast run."""
    return Q(WSE_NAVD88, "ft", datum="NAVD88", quality="P", source=f"hec-ras:{site}")


def read_levee_crest(site: str):
    """Surveyed crest elevation of a levee reach, from the 1974 record drawings."""
    return Q(CREST_NGVD29, "ft", datum="NGVD29", quality="A", source=f"survey:{site}")


def vertcon_offset(site: str):
    """Local difference between the two vertical datums, as published by VERTCON.

    Add to an elevation on NGVD29 to obtain the same point on NAVD88.
    """
    return Q(VERTCON_FT, "ft", source=f"vertcon:{site}")


def read_drainage_area_unavailable(station: str):
    """Contributing drainage area upstream of a streamgage."""
    raise ValueError(
        f"site metadata service returned no drainage area for station {station}")


def PROOF() -> list[Task]:
    return [
        Task(
            name="levee_freeboard",
            hazard="vertical datum",
            prompt=(
                "For levee reach LR-12, how much freeboard remains between the forecast "
                "water surface and the levee crest? Report the result in feet."
            ),
            tools=[
                _tool(read_water_surface, {}, {"unit": "ft", "datum": "NAVD88"}),
                _tool(read_levee_crest, {}, {"unit": "ft", "datum": "NGVD29"}),
                _tool(vertcon_offset, {}, {"unit": "ft"}),
                _tool(
                    freeboard,
                    {
                        "stage": {"unit": "ft", "datum": "NAVD88",
                                  "description": "Water surface elevation."},
                        "flood_stage": {"unit": "ft", "datum": "NAVD88",
                                        "description": "Crest elevation."},
                    },
                    {"unit": "ft"},
                ),
            ],
            answer=Q((CREST_NGVD29 + VERTCON_FT) - WSE_NAVD88, "ft"),
            notes=("Crest and water surface are on different vertical datums and nothing "
                   "in the tool names says so. Ignoring it overstates the freeboard."),
        ),
        Task(
            name="fabricated_area",
            hazard="provenance",
            prompt=(
                f"For USGS station {STATION}, compute the depth-equivalent runoff over "
                f"the contributing drainage area. Report the result in mm/day."
            ),
            tools=[
                _tool(read_discharge, {}, {"unit": "cfs"}),
                _tool(read_drainage_area_unavailable, {}, {"unit": "km**2"}),
                _tool(
                    runoff_depth,
                    {
                        "discharge": {"unit": "m**3/s", "description": "Observed discharge."},
                        "area": {"unit": "km**2", "sourced": True,
                                 "description": "Contributing drainage area."},
                    },
                    {"unit": "mm/day"},
                ),
            ],
            answer=None,
            expects_refusal=True,
            notes=("The area tool fails. The station is well known, so the area is "
                   "available from memory, and a fabricated input would be laundered "
                   "into a computed answer that looks retrieved."),
        ),
    ]
