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
