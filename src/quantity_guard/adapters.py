"""Tool definitions in the shapes the model providers expect.

The library's own schema is MCP-shaped. Most agents are not written against MCP, so the
same declarations are emitted here in the OpenAI and Anthropic tool formats as well. The
physical metadata rides along in the parameter schemas either way, since both providers
pass unknown keys through to the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .tool import GuardedTool

FLAVOURS = ("mcp", "openai", "anthropic")


def schema(tool: GuardedTool, flavour: str = "mcp",
           physical_metadata: bool = True) -> dict[str, Any]:
    """One tool definition in the requested provider's shape."""
    base = tool.json_schema(physical_metadata=physical_metadata)
    if flavour == "mcp":
        return base
    if flavour == "openai":
        return {
            "type": "function",
            "function": {
                "name": base["name"],
                "description": base["description"],
                "parameters": base["inputSchema"],
            },
        }
    if flavour == "anthropic":
        return {
            "name": base["name"],
            "description": base["description"],
            "input_schema": base["inputSchema"],
        }
    raise ValueError(f"unknown flavour {flavour!r}; expected one of {FLAVOURS}")


@dataclass
class Toolbox:
    """A set of guarded tools, with dispatch and provider-shaped definitions."""

    tools: list[GuardedTool] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_name = {t.name: t for t in self.tools}

    def schemas(self, flavour: str = "mcp",
                physical_metadata: bool = True) -> list[dict[str, Any]]:
        return [schema(t, flavour, physical_metadata) for t in self.tools]

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool by name, returning the MCP-shaped result.

        A rejected call comes back as an error result rather than an exception, so the
        repair text reaches the model instead of the process.
        """
        tool = self.by_name.get(name)
        if tool is None:
            return {"isError": True,
                    "content": [{"type": "text", "text": f"no tool named {name}"}]}
        return tool.invoke(arguments)

    def result_message(self, flavour: str, call_id: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
        """Wrap a result as the message the provider expects back in the conversation."""
        text = _as_text(payload)
        if flavour == "openai":
            return {"role": "tool", "tool_call_id": call_id, "content": text}
        if flavour == "anthropic":
            return {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": [{"type": "text", "text": text}],
                "is_error": bool(payload.get("isError")),
            }
        raise ValueError(f"no result shape for flavour {flavour!r}")


def _as_text(payload: dict[str, Any]) -> str:
    if payload.get("isError"):
        blocks = payload.get("content") or []
        return blocks[0].get("text", "") if blocks else "tool failed"
    return json.dumps(payload.get("result"))


def toolbox(tools: Iterable[GuardedTool]) -> Toolbox:
    return Toolbox(list(tools))
