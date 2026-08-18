"""Tool definitions in the shapes the model providers expect.

The library's own schema is MCP-shaped. Most agents are not written against MCP, so the
same declarations are emitted here in the OpenAI and Anthropic tool formats as well. The
physical metadata rides along in the parameter schemas either way, since both providers
pass unknown keys through to the model.

The exception is OpenAI's strict mode, which validates the schema itself and rejects
keywords it does not recognise. Pass ``strict=True`` for that dialect; see :func:`schema`
for what moves where.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .tool import GuardedTool

FLAVOURS = ("mcp", "openai", "anthropic")


def schema(tool: GuardedTool, flavour: str = "mcp",
           physical_metadata: bool = True, strict: bool = False) -> dict[str, Any]:
    """One tool definition in the requested provider's shape.

    ``strict`` emits OpenAI's strict function schema, which is a restricted dialect: it
    admits no keyword it does not know, so the ``x-unit`` and ``x-datum`` extensions are
    moved into the description rather than sent as keys, and it requires every property
    to be listed in ``required``, so an optional parameter is expressed as nullable
    instead. The declarations survive; only the encoding changes.
    """
    if strict and flavour != "openai":
        raise ValueError(
            f"strict is an OpenAI function-calling mode and has no meaning for "
            f"{flavour!r}; the {flavour!r} schema is already accepted as emitted"
        )
    base = tool.json_schema(physical_metadata=physical_metadata)
    if flavour == "mcp":
        return base
    if flavour == "openai":
        function: dict[str, Any] = {
            "name": base["name"],
            "description": base["description"],
            "parameters": _strict_parameters(base["inputSchema"]) if strict
            else base["inputSchema"],
        }
        if strict:
            function["strict"] = True
        return {"type": "function", "function": function}
    if flavour == "anthropic":
        return {
            "name": base["name"],
            "description": base["description"],
            "input_schema": base["inputSchema"],
        }
    raise ValueError(f"unknown flavour {flavour!r}; expected one of {FLAVOURS}")


#: What an undeclared parameter is allowed to be under a dialect that demands a type of
#: everything. Anything JSON can hold as a single value, since nothing has been said.
_UNTYPED = [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]


def _strict_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    """The same parameters, in the subset of JSON Schema strict mode accepts."""
    required = set(input_schema.get("required", []))
    properties = {
        name: _strict_property(prop, optional=name not in required)
        for name, prop in input_schema.get("properties", {}).items()
    }
    return {
        "type": "object",
        "properties": properties,
        # Strict mode requires every property to be listed. Optionality is carried by the
        # value being allowed to be null, which is the substitution OpenAI documents.
        "required": list(properties),
        "additionalProperties": False,
    }


def _strict_property(prop: dict[str, Any], optional: bool) -> dict[str, Any]:
    described = prop.get("description", "")
    unit = prop.get("x-unit")
    frames = [f"{label} {prop[key]}." for key, label in (
        ("x-datum", "Referenced to"), ("x-crs", "In CRS"), ("x-tz", "Timezone"),
    ) if prop.get(key)]
    # The physical declaration cannot ride in a key here, so it rides in the prose the
    # model reads anyway. Dropping it silently would leave the parameter looking like a
    # bare number, which is the state this library exists to move tools out of. Notes the
    # description already makes are not made twice.
    notes = [note for note in ([f"In {unit}." if unit else ""] + frames)
             if note and note not in described]
    out = _all_properties_required(_without_extensions(prop))
    if notes:
        out["description"] = " ".join([described] + notes).strip()
    if "type" not in out and "anyOf" not in out:
        out["anyOf"] = list(_UNTYPED)
    return _nullable(out) if optional else out


def _all_properties_required(value: Any) -> Any:
    """Every object schema, at any depth, listing all of its properties as required.

    Strict mode admits no partially required object, so optionality is expressed by the
    value being allowed to be null. That reads the same way the validator behaves: the
    unit key on a quantity object may be absent or null, and either way the declared unit
    stands in.
    """
    if isinstance(value, list):
        return [_all_properties_required(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _all_properties_required(v) for k, v in value.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        required = set(out.get("required", []))
        out["properties"] = {
            name: prop if name in required else _nullable(prop)
            for name, prop in out["properties"].items()
        }
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _without_extensions(value: Any) -> Any:
    """The schema with every ``x-`` extension removed, at any depth."""
    if isinstance(value, dict):
        return {k: _without_extensions(v) for k, v in value.items()
                if not k.startswith("x-")}
    if isinstance(value, list):
        return [_without_extensions(v) for v in value]
    return value


def _nullable(prop: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in prop:
        return {**prop, "anyOf": [*prop["anyOf"], {"type": "null"}]}
    if "type" not in prop:
        return {**prop, "anyOf": [*_UNTYPED, {"type": "null"}]}
    declared = prop["type"]
    types = declared if isinstance(declared, list) else [declared]
    return {**prop, "type": [*types, "null"]}


@dataclass
class Toolbox:
    """A set of guarded tools, with dispatch and provider-shaped definitions."""

    tools: list[GuardedTool] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_name = {t.name: t for t in self.tools}

    def schemas(self, flavour: str = "mcp", physical_metadata: bool = True,
                strict: bool = False) -> list[dict[str, Any]]:
        return [schema(t, flavour, physical_metadata, strict) for t in self.tools]

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
