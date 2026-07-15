"""Physical declarations for tools you did not write.

An existing MCP server states its units in prose, if at all. An annotation file supplies
the missing declarations from outside, keyed by tool and parameter name, so a server can
be guarded without editing it.

TOML is read with the standard library on Python 3.11 and later; JSON is read everywhere.

    [tools.read_discharge]
    returns = { unit = "cfs" }

    [tools.runoff_depth.params]
    discharge = { unit = "m**3/s", description = "Observed discharge." }
    area = { unit = "km**2" }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec import Spec

try:  # Python 3.11 and later
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    tomllib = None


@dataclass
class ToolAnnotation:
    """Declared physical types for one upstream tool."""

    params: dict[str, Spec] = field(default_factory=dict)
    returns: Spec | None = None

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> ToolAnnotation:
        unknown = set(obj) - {"params", "returns"}
        if unknown:
            raise ValueError(f"unknown annotation keys: {sorted(unknown)}")
        params = {k: Spec.coerce_spec(v) for k, v in (obj.get("params") or {}).items()}
        returns = obj.get("returns")
        return cls(params=params, returns=Spec.coerce_spec(returns) if returns else None)


def load(path: str | Path) -> dict[str, ToolAnnotation]:
    """Read an annotation file into a mapping of tool name to declarations."""
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        raw = json.loads(text)
    elif tomllib is not None:
        raw = tomllib.loads(text)
    else:
        raise RuntimeError(
            "TOML annotations need Python 3.11 or later; use a .json file instead"
        )
    return parse(raw)


def parse(raw: dict[str, Any]) -> dict[str, ToolAnnotation]:
    tools = raw.get("tools", raw)
    return {name: ToolAnnotation.from_obj(obj) for name, obj in tools.items()}
