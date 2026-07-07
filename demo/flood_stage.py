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


def raw_freeboard(stage_value, flood_stage_value):
    """Both in feet."""
    return flood_stage_value - stage_value


# Scenarios -------------------------------------------------------------------------------


def scenario_one() -> None:
    """The model reuses a magnitude from a cfs tool in a tool that expects m3/s."""
    print(RULE)
    print("1. A magnitude carried between tools without its unit")
    print(RULE)
    print("The gage returns 1250 cfs. The model passes the bare number 1250 into a tool")
    print("whose discharge parameter is declared in m3/s.\n")

    unguarded = raw_runoff_depth(1250.0, 29000.0)
    correct = raw_runoff_depth(35.396, 29000.0)
    print(f"  unguarded  runoff depth = {unguarded:.4f} mm/day")
    print(f"  correct    runoff depth = {correct:.4f} mm/day")
    print(f"  the unguarded result is high by a factor of {unguarded / correct:.1f}\n")

    with session() as s:
        read_discharge(STATION)
        result = runoff_depth.invoke({"discharge": 1250, "area": 29000})
        print("  guarded:")
        print(f"    {result['content'][0]['text']}\n")
        repaired = runoff_depth.invoke(
            {"discharge": {"value": 1250, "unit": "cfs"}, "area": 29000}
        )
        print(f"  after the model applies the repair: {repaired['result']['value']:.4f} mm/day")
        _ = s


def scenario_two() -> None:
    """The model compares a local gage datum against NAVD88."""
    print("\n" + RULE)
    print("2. Two elevations differenced across different vertical datums")
    print(RULE)
    print("Gage height is 12.4 ft on the station datum, flood stage is 31.0 ft on NAVD88,")
    print("and the station's zero point sits 1.5 ft above NAVD88. Both values are lengths,")
    print("so dimensional analysis alone permits the subtraction.\n")

    print(f"  unguarded  freeboard = {raw_freeboard(12.4, 31.0):.1f} ft")
    print(f"  correct    freeboard = {raw_freeboard(12.4 + 1.5, 31.0):.1f} ft")
    print("  the unguarded result overstates the margin by the datum offset\n")

    with session():
        stage = read_gage_height(STATION)
        flood = read_flood_stage(STATION)
        result = freeboard.invoke({"stage": stage, "flood_stage": flood})
        print("  guarded:")
        print(f"    {result['content'][0]['text']}\n")
        repaired = freeboard.invoke(
            {"stage": stage.to_datum("NAVD88"), "flood_stage": flood}
        )
        print(f"  after shifting the stage to NAVD88: {repaired['result']['value']:.1f} ft")


def scenario_three() -> None:
    """The model states a figure it never retrieved."""
    print("\n" + RULE)
    print("3. A quantitative claim that no tool produced")
    print(RULE)
    print("The model writes a summary. Every figure in it is well formed, and one of them")
    print("was never returned by any tool.\n")

    answer = (
        "At station 07374000 the river is currently at 12.4 ft on the gage datum, "
        "against a flood stage of 31.0 ft NAVD88, leaving 17.1 ft of freeboard. "
        "Observed discharge is 1250 cfs and the peak is forecast to reach 4200 cfs "
        "within 36 hours."
    )

    with session() as s:
        stage = read_gage_height(STATION)
        flood = read_flood_stage(STATION)
        read_discharge(STATION)
        margin = freeboard(stage.to_datum("NAVD88"), flood)
        s.record_derived(margin, note="freeboard")

        audit = s.audit_answer(answer)

    print(f"  answer: {answer}\n")
    print("  audit:")
    print(audit.report())
    print()
    for claim in audit.unsourced:
        print(f"  {claim.text} appears in the answer and traces to no tool output.")
    print(f"\n  session manifest holds {len(s.manifest()['quantities'])} recorded quantities")


def scenario_four() -> None:
    """A correct magnitude reported in the wrong unit."""
    print("\n" + RULE)
    print("4. A correct magnitude reported in the wrong unit")
    print(RULE)

    with session() as s:
        read_discharge(STATION)
        audit = s.audit_answer("Observed discharge is 1250 m3/s.")

    print("  answer: Observed discharge is 1250 m3/s.\n  audit:")
    print(audit.report())


def main() -> None:
    print("\nquantity-guard: agent numeric failure modes at a USGS streamgage")
    print(f"station {STATION}, gage datum {GAGE_DATUM} at 1.5 ft NAVD88\n")
    scenario_one()
    scenario_two()
    scenario_three()
    scenario_four()
    print("\n" + RULE)
    print("Each failure is refused at the boundary with a message the model can act on,")
    print("rather than propagating into the answer as a well-formed number.")
    print(RULE + "\n")


if __name__ == "__main__":
    main()
