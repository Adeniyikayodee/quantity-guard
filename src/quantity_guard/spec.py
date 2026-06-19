"""Parameter specifications.

A ``Spec`` declares what a tool parameter physically is, which serves two purposes: it
generates the JSON Schema the model reads before calling, and it validates and normalises
whatever the model actually sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pint

from .errors import (
    CRSMismatch,
    DatumMismatch,
    DimensionalityError,
    MissingUnit,
    QualityViolation,
    TimezoneError,
    UnitParseError,
)
from .quantity import Q
from .registry import QUALITY_RANK, normalize_quality, ureg


@dataclass(frozen=True)
class Spec:
    """Declared physical type of a tool parameter or return value.

    ``quality`` sets the weakest record the tool will accept, so a tool declaring
    ``quality="approved"`` rejects provisional input rather than silently propagating it.
    """

    unit: str | None = None
    datum: str | None = None
    crs: str | None = None
    tz: str | None = None
    quality: str | None = None
    description: str = ""
    require_explicit_unit: bool = False

    @property
    def is_temporal(self) -> bool:
        return self.unit is None and self.tz is not None

    @property
    def is_physical(self) -> bool:
        """False for a spec that declares no unit and no timezone, such as an identifier."""
        return self.unit is not None or self.tz is not None

    @classmethod
    def coerce_spec(cls, obj: Any) -> Spec:
        if isinstance(obj, Spec):
            return obj
        if isinstance(obj, str):
            return cls(unit=obj)
        if isinstance(obj, dict):
            return cls(**obj)
        raise TypeError(f"cannot read a Spec from {type(obj).__name__}")

    # Validation ------------------------------------------------------------------------
