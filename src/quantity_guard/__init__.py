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
    UnsourcedNumber,
)
from .provenance import AnswerAudit, NumberClaim, Session, session
from .quantity import Q
from .registry import datums, ureg
from .spec import Spec
from .tool import GuardedTool, quantity_tool

__version__ = "0.3.1"

__all__ = [
    "Q",
    "Spec",
    "quantity_tool",
    "GuardedTool",
    "session",
    "Session",
    "AnswerAudit",
    "NumberClaim",
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
    "UnsourcedNumber",
]
