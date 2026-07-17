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

    def _validate(self, tool: str, note: ToolAnnotation,
                  arguments: dict[str, Any]) -> dict[str, Any]:
        """Check each declared argument and rewrite it in the unit upstream expects."""
        for name, spec in note.params.items():
            if name not in arguments:
                continue
            raw = arguments[name]
            value = spec.coerce(raw, field=name)
            if isinstance(value, Q):
                if self.ledger is not None:
                    found = self.ledger.detect_carry_over(raw, value)
                    if found is not None:
                        raise GuardViolation(
                            carry_over_message(raw, value, found), field=name)
                    self.ledger.record(tool, "input", name, value)
                arguments[name] = value.magnitude
            elif isinstance(value, datetime):
                arguments[name] = value.isoformat()
        return arguments

    def _annotate(self, tool: str, note: ToolAnnotation,
                  result: dict[str, Any]) -> dict[str, Any]:
        """Restate a bare numeric result with its unit, and record it."""
        if note.returns is None or result.get("isError"):
            return result
        magnitude = _read_number(result)
        if magnitude is None:
            return result
        quantity = Q(magnitude, note.returns.unit, datum=note.returns.datum)
        if self.ledger is not None:
            self.ledger.record(tool, "output", "return", quantity)
        return {
            **result,
            "content": [{"type": "text", "text": json.dumps(quantity.as_dict())}],
        }


def _read_number(result: dict[str, Any]) -> float | None:
    """The single number in a tool result, if it holds one."""
    for block in result.get("content") or []:
        if block.get("type") != "text":
            continue
        text = (block.get("text") or "").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                return float(text)
            except ValueError:
                return None
        if isinstance(payload, (int, float)) and not isinstance(payload, bool):
            return float(payload)
        if isinstance(payload, dict) and isinstance(payload.get("value"), (int, float)):
            return float(payload["value"])
        return None
    return None


# Talking to a child server over stdio -----------------------------------------------------


class StdioUpstream:
    """An MCP server run as a child process, addressed over newline-delimited JSON-RPC."""

    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._id = 0
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "quantity-guard", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"upstream server closed while awaiting {method}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers sometimes log to stdout; skip anything unparseable
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"upstream error on {method}: {message['error']}")
                return message.get("result", {})

    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.terminate()


# Serving ----------------------------------------------------------------------------------


def serve_stdio(proxy: GuardedProxy, stdin=None, stdout=None) -> None:
    """Answer JSON-RPC on stdin, so the proxy is itself an MCP server."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # a notification; nothing to answer

        try:
            result = _dispatch(proxy, method, message.get("params") or {})
        except Exception as exc:  # a protocol-level failure, not a tool failure
            reply = {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32603, "message": str(exc)}}
        else:
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def _dispatch(proxy: GuardedProxy, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "quantity-guard", "version": "0.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": proxy.list_tools()}
    if method == "tools/call":
        return proxy.call_tool(params.get("name", ""), params.get("arguments") or {})
    raise ValueError(f"unsupported method: {method}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quantity-guard-mcp",
        description="Guard an existing MCP server with declared physical types.",
    )
    parser.add_argument("--annotations", required=True,
                        help="TOML or JSON file declaring units per tool and parameter")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="the upstream server command, after --")
    args = parser.parse_args(argv)

    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("give the upstream server command after --")

    notes = annotations_module.load(args.annotations)
    upstream = StdioUpstream(command)
    try:
        with open_session() as ledger:
            serve_stdio(GuardedProxy(upstream, notes, ledger))
    finally:
        upstream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
