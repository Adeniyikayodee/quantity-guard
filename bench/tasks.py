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
HOURLY_CFS = {6: 980.0, 9: 1250.0, 14: 1640.0, 19: 1370.0}


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
