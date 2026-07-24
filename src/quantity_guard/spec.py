"""Parameter specifications.

A ``Spec`` declares what a tool parameter physically is, which serves two purposes: it
generates the JSON Schema the model reads before calling, and it validates and normalises
whatever the model actually sends.
"""

from __future__ import annotations

import json
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

    def _to_quantity(self, value: Any, field: str) -> Q:
        if isinstance(value, Q):
            return value
        if isinstance(value, dict):
            if "value" not in value:
                raise UnitParseError(
                    f"quantity object needs a 'value' key, received keys "
                    f"{sorted(value)}",
                    field=field,
                )
            unit = value.get("unit") or self.unit
            if unit is None:
                raise MissingUnit(f"no unit given and none declared", field=field)
            return Q(
                value["value"],
                unit,
                datum=value.get("datum"),
                crs=value.get("crs"),
                quality=value.get("quality"),
                source=value.get("source"),
            )
        if isinstance(value, str):
            # Models routinely serialise the object form into the string form. Reading it
            # back is unambiguous and avoids rejecting a call that carried its unit
            # correctly, just encoded one level deeper than expected.
            text = value.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    return self._to_quantity(json.loads(text), field)
                except json.JSONDecodeError:
                    pass
            return Q.parse(value)
        if isinstance(value, (list, tuple)) or hasattr(value, "__array__"):
            if self.unit is None:
                raise MissingUnit("no unit declared for this parameter", field=field)
            return Q(value, self.unit, datum=self.datum)
        if isinstance(value, (int, float)):
            if self.require_explicit_unit:
                raise MissingUnit(
                    f"this tool requires an explicit unit, so send "
                    f'{{"value": {value}, "unit": "<unit>"}} rather than a bare number',
                    field=field,
                )
            if self.unit is None:
                raise MissingUnit("no unit declared for this parameter", field=field)
            # A bare number is the declared unit by contract, since the schema states it.
            return Q(value, self.unit, datum=self.datum)
        raise UnitParseError(
            f"cannot read a quantity from {type(value).__name__}", field=field
        )

    def _check_datum(self, quantity: Q, field: str) -> Q:
        if not self.datum:
            return quantity
        if quantity.datum is None:
            # Trusting the declared datum here is the contract, as with bare units.
            return Q(
                quantity.magnitude,
                quantity.units,
                datum=self.datum,
                crs=quantity.crs,
                quality=quantity.quality,
                source=quantity.source,
            )
        if quantity.datum == self.datum:
            return quantity
        raise DatumMismatch(
            f"expected an elevation referenced to {self.datum}, received one referenced "
            f"to {quantity.datum}; both are in compatible units but measured from "
            f"different references, so resend the value on {self.datum} or register the "
            f"local offset between them",
            field=field,
            expected=self.datum,
            received=quantity.datum,
        )

    @staticmethod
    def _dimension_name(unit: Any) -> str:
        try:
            dims = ureg.Quantity(1, unit).dimensionality
        except (pint.errors.UndefinedUnitError, TypeError):
            return "unknown"
        return " ".join(f"{k}^{v:g}" if v != 1 else k for k, v in dims.items()) or "dimensionless"

    # Schema ----------------------------------------------------------------------------

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema fragment, extended with the physical metadata the model needs."""
        if not self.is_physical:
            return {"description": self.description}

        if self.is_temporal:
            schema: dict[str, Any] = {
                "type": "string",
                "format": "date-time",
                "description": (
                    f"{self.description} ISO 8601 with an explicit UTC offset, "
                    f"interpreted in {self.tz}."
                ).strip(),
                "x-tz": self.tz,
            }
            return schema

        obj_props: dict[str, Any] = {
            "value": {"type": "number"},
            "unit": {"type": "string", "description": f"defaults to {self.unit}"},
        }
        if self.datum:
            obj_props["datum"] = {"type": "string", "description": f"defaults to {self.datum}"}
        if self.crs:
            obj_props["crs"] = {"type": "string"}
        obj_props["quality"] = {"type": "string", "enum": sorted(QUALITY_RANK)}

        variants: list[dict[str, Any]] = []
        if not self.require_explicit_unit:
            variants.append({"type": "number", "description": f"magnitude in {self.unit}"})
        variants.append(
            {
                "type": "object",
                "properties": obj_props,
                "required": ["value", "unit"],
                "additionalProperties": False,
            }
        )
        variants.append({"type": "string", "description": f'quantity with unit, e.g. "1.5 {self.unit}"'})

        described = self.description
        if self.datum:
            described += f" Referenced to {self.datum}."
        if self.quality:
            described += f" Requires {self.quality} record or better."

        schema = {"description": described.strip(), "x-unit": self.unit, "oneOf": variants}
        if self.datum:
            schema["x-datum"] = self.datum
        if self.crs:
            schema["x-crs"] = self.crs
        if self.quality:
            schema["x-quality"] = self.quality
        return schema
