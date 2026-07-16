"""Guard an MCP server you did not write.

The proxy sits between an agent and an existing MCP server. It reads the server's tool
list, merges in the physical declarations from an annotation file, and re-advertises the
tools with their units stated in the schema. Calls are validated on the way through and
converted into the unit the upstream server already expects, so the server itself needs
no change.

    quantity-guard-mcp --annotations water.toml -- python -m my_server

The conversion is the point. When the model sends 1250 cfs to a parameter declared in
m**3/s, the upstream tool receives 35.4, which is the number it was always expecting.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import annotations as annotations_module
from .annotations import ToolAnnotation
from .errors import GuardViolation
from .provenance import Session, carry_over_message, session as open_session
from .quantity import Q
from .spec import Spec

PROTOCOL_VERSION = "2025-06-18"


class Upstream(Protocol):
    """The part of an MCP server the proxy needs."""

    def list_tools(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


# Middleware -----------------------------------------------------------------------------


@dataclass
class GuardedProxy:
    """Validating middleware around an upstream MCP server."""

    upstream: Upstream
    annotations: dict[str, ToolAnnotation]
    ledger: Session | None = None

    def list_tools(self) -> list[dict[str, Any]]:
        """Upstream tools, re-advertised with their physical types declared."""
        return [self._enrich(tool) for tool in self.upstream.list_tools()]

    def _enrich(self, tool: dict[str, Any]) -> dict[str, Any]:
        note = self.annotations.get(tool.get("name", ""))
        if note is None or not note.params:
            return tool
        tool = json.loads(json.dumps(tool))
        schema = tool.setdefault("inputSchema", {"type": "object", "properties": {}})
        properties = schema.setdefault("properties", {})
        for name, spec in note.params.items():
            original = properties.get(name, {})
            declared = spec.json_schema()
            # Keep whatever the server already said about the parameter; the declaration
            # adds the physical type rather than replacing the explanation.
            existing = original.get("description", "")
            if existing and existing not in declared.get("description", ""):
                declared["description"] = f"{existing} {declared.get('description', '')}".strip()
            properties[name] = declared
        return tool

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        note = self.annotations.get(name)
        if note is None:
            return self.upstream.call_tool(name, arguments)
        try:
            forwarded = self._validate(name, note, dict(arguments))
        except GuardViolation as violation:
            return {
                "content": [{"type": "text", "text": violation.repair()}],
                "isError": True,
            }
        result = self.upstream.call_tool(name, forwarded)
        return self._annotate(name, note, result)
