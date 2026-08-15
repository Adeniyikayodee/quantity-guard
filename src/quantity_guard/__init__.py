"""quantity-guard: typed physical quantities at the AI agent tool boundary.

Numbers crossing into and out of an agent's tools carry their unit, vertical datum,
coordinate reference system, and record quality as structured metadata that the runtime
enforces, rather than as prose the model is expected to track.
"""

from .errors import (
    CRSMismatch,
    DatumConversionUnavailable,
    DatumMismatch,
    DimensionalityError,
    GuardViolation,
    MissingUnit,
    QualityViolation,
    TimezoneError,
    UnconvertedCarryOver,
    UnitParseError,
    UnsourcedInput,
    UnsourcedNumber,
)
from .adapters import Toolbox, schema, toolbox
from .provenance import AnswerAudit, CarryOver, NumberClaim, Session, WouldBlock, session
from .quantity import Q
from .registry import datums, ureg
from .spec import Spec
from .tool import GuardedTool, quantity_tool

__version__ = "0.5.0"

__all__ = [
    "Q",
    "Spec",
    "quantity_tool",
    "GuardedTool",
    "session",
    "Session",
    "AnswerAudit",
    "NumberClaim",
    "CarryOver",
    "WouldBlock",
    "Toolbox",
    "toolbox",
    "schema",
    "datums",
    "ureg",
    "GuardViolation",
    "DimensionalityError",
    "DatumMismatch",
    "DatumConversionUnavailable",
    "CRSMismatch",
    "TimezoneError",
    "QualityViolation",
    "MissingUnit",
    "UnconvertedCarryOver",
    "UnitParseError",
    "UnsourcedInput",
    "UnsourcedNumber",
]
