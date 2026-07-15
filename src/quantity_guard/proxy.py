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
