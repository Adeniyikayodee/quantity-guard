"""Provenance ledger and answer auditing.

Guarded tools record every quantity that crosses their boundary. Auditing an answer then
asks a narrow question of each number in the text: does it trace to something a tool
actually returned, and is it stated in the unit it was returned in.

This addresses the failure mode in which a model bypasses the tool path and produces a
plausible figure directly, which conventional output validation cannot detect because the
value is well-formed.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import pint

from .quantity import Q
from .registry import ureg

_ACTIVE: list["Session"] = []


def active_session() -> "Session | None":
    return _ACTIVE[-1] if _ACTIVE else None


@dataclass
class LedgerEntry:
    tool: str
    role: str  # "input", "output", or "derived"
    field: str
    quantity: Q
    call_id: int
    note: str = ""


@dataclass
class CarryOver:
    """A prior output whose magnitude reappeared without its reference frame."""

    entry: LedgerEntry
    reason: str  # "unit" or "datum"


@dataclass
class Session:
    """Records quantities crossing tool boundaries within a block of agent work."""

    entries: list[LedgerEntry] = field(default_factory=list)
    calls: int = 0
    started: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def record(self, tool: str, role: str, name: str, quantity: Q, note: str = "") -> None:
        self.entries.append(
            LedgerEntry(tool=tool, role=role, field=name, quantity=quantity, call_id=self.calls, note=note)
        )
