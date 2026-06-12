"""Unit and datum registries.

`pint` supplies dimensional analysis, while the datum registry supplies what `pint`
structurally cannot: reference frames that share a unit without sharing a meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pint

from .errors import DatumConversionUnavailable, DatumMismatch

# Process-wide singleton, since quantities built from different pint registries cannot
# interoperate.
ureg = pint.UnitRegistry()

# Units in common engineering and hydrology use that pint does not ship.
_EXTRA_UNITS = [
    "cfs = foot ** 3 / second",
    "cms = meter ** 3 / second",
    "mgd = 1e6 * gallon / day",
    "sfd = foot ** 3 / second * day",
]

for _defn in _EXTRA_UNITS:
    try:
        ureg.define(_defn)
    except (pint.errors.RedefinitionError, pint.errors.DefinitionSyntaxError):
        pass


# Quality flags -------------------------------------------------------------------------

# Higher rank means less trustworthy. Combining quantities takes the worst known rank, so
# any computation touching provisional record yields a provisional answer.
QUALITY_RANK: dict[str, int] = {
    "approved": 0,
    "reviewed": 0,
    "estimated": 1,
    "provisional": 2,
    "unverified": 3,
}

# Single-letter codes as published on USGS time series.
QUALITY_ALIASES: dict[str, str] = {
    "A": "approved",
    "R": "reviewed",
    "e": "estimated",
    "E": "estimated",
    "P": "provisional",
    "p": "provisional",
}


def normalize_quality(flag: str | None) -> str | None:
    if flag is None:
        return None
    flag = QUALITY_ALIASES.get(flag, str(flag).strip().lower())
    if flag not in QUALITY_RANK:
        raise ValueError(f"unknown quality flag {flag!r}, known: {sorted(QUALITY_RANK)}")
    return flag


def worst_quality(*flags: str | None) -> str | None:
    known = [f for f in flags if f is not None]
    return max(known, key=lambda f: QUALITY_RANK[f]) if known else None


# Datums --------------------------------------------------------------------------------
