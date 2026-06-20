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

    def coerce(self, value: Any, field: str) -> Any:
        if not self.is_physical:
            return value
        return self._coerce_time(value, field) if self.is_temporal else self._coerce_quantity(value, field)

    def _coerce_time(self, value: Any, field: str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise TimezoneError(
                    f"cannot read {value!r} as a timestamp, send ISO 8601 with an offset "
                    f"such as '2026-08-14T09:30:00-05:00'",
                    field=field,
                ) from None
        if not isinstance(value, datetime):
            raise TimezoneError(
                f"expected a timestamp, received {type(value).__name__}", field=field
            )
        if value.tzinfo is None:
            raise TimezoneError(
                f"timestamp {value.isoformat()} is timezone-naive, and gage records are "
                f"published in local standard time while models default to UTC, so the "
                f"offset must be explicit; resend as ISO 8601 with an offset",
                field=field,
            )
        target = timezone.utc if self.tz.upper() == "UTC" else self._zone(field)
        return value.astimezone(target)

    def _zone(self, field: str) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise TimezoneError(f"unknown timezone {self.tz!r}", field=field) from None

    def _coerce_quantity(self, value: Any, field: str) -> Q:
        quantity = self._to_quantity(value, field)
        quantity = self._check_datum(quantity, field)

        if self.crs and quantity.crs and quantity.crs != self.crs:
            raise CRSMismatch(
                f"expected {self.crs}, received {quantity.crs}", field=field
            )

        if self.quality and quantity.quality:
            if QUALITY_RANK[quantity.quality] > QUALITY_RANK[normalize_quality(self.quality)]:
                raise QualityViolation(
                    f"this tool requires {self.quality} record, and the value supplied is "
                    f"{quantity.quality}",
                    field=field,
                )

        if self.unit:
            try:
                quantity = quantity.to(self.unit)
            except DimensionalityError:
                raise DimensionalityError(
                    f"expected a quantity in {self.unit} "
                    f"({self._dimension_name(self.unit)}), received {quantity:~} "
                    f"({self._dimension_name(quantity.units)}); these are different "
                    f"physical quantities and no conversion exists",
                    field=field,
                ) from None
        return quantity
