import json
import subprocess
import sys
from pathlib import Path

import pytest

from quantity_guard import Session, session
from quantity_guard.annotations import parse
from quantity_guard.proxy import GuardedProxy, StdioUpstream, serve_stdio

NOTES = parse({
    "tools": {
        "read_discharge": {"returns": {"unit": "cfs"}},
        "runoff_depth": {
            "params": {
                "discharge": {"unit": "m**3/s", "description": "Observed discharge."},
                "area": {"unit": "km**2"},
            },
            "returns": {"unit": "mm/day"},
        },
    }
})


class FakeUpstream:
    """An MCP server that speaks in bare numbers, as most do."""

    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [
            {"name": "read_discharge", "description": "Latest discharge.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "runoff_depth", "description": "Runoff depth.",
             "inputSchema": {"type": "object", "properties": {
                 "discharge": {"type": "number", "description": "Flow rate."},
                 "area": {"type": "number"}}}},
            {"name": "unannotated", "description": "Left alone.",
             "inputSchema": {"type": "object", "properties": {"x": {"type": "number"}}}},
        ]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "read_discharge":
            return {"content": [{"type": "text", "text": "1250.0"}]}
        if name == "runoff_depth":
            depth = arguments["discharge"] / (arguments["area"] * 1e6) * 1000 * 86400
            return {"content": [{"type": "text", "text": json.dumps(depth)}]}
        return {"content": [{"type": "text", "text": "ok"}]}


def make(ledger=None):
    upstream = FakeUpstream()
    return upstream, GuardedProxy(upstream, NOTES, ledger)


# Schema ---------------------------------------------------------------------------------


def test_declared_parameters_gain_their_unit_in_the_schema():
    _, proxy = make()
    tools = {t["name"]: t for t in proxy.list_tools()}
    discharge = tools["runoff_depth"]["inputSchema"]["properties"]["discharge"]
    assert discharge["x-unit"] == "m**3/s"
    # The server's own wording survives alongside the declaration.
    assert "Flow rate." in discharge["description"]


def test_unannotated_tools_pass_through_untouched():
    upstream, proxy = make()
    original = {t["name"]: t for t in upstream.list_tools()}["unannotated"]
    seen = {t["name"]: t for t in proxy.list_tools()}["unannotated"]
    assert seen == original


# Calls ----------------------------------------------------------------------------------


def test_the_upstream_receives_the_unit_it_expects():
    upstream, proxy = make()
    proxy.call_tool("runoff_depth", {"discharge": "1250 cfs", "area": "29000 km**2"})
    name, arguments = upstream.calls[-1]
    assert name == "runoff_depth"
    # 1250 cfs converted for a server that only ever understood m**3/s.
    assert arguments["discharge"] == pytest.approx(35.396, rel=1e-3)
    assert arguments["area"] == pytest.approx(29000)


def test_a_wrong_dimension_never_reaches_the_upstream():
    upstream, proxy = make()
    result = proxy.call_tool("runoff_depth", {"discharge": "12.4 ft", "area": 29000})
    assert result["isError"] is True
    assert "no conversion exists" in result["content"][0]["text"]
    assert upstream.calls == []


def test_a_bare_result_comes_back_with_its_unit():
    _, proxy = make()
    result = proxy.call_tool("read_discharge", {})
    assert json.loads(result["content"][0]["text"]) == {"value": 1250.0, "unit": "cfs"}


def test_an_unannotated_tool_is_forwarded_unchanged():
    upstream, proxy = make()
    proxy.call_tool("unannotated", {"x": 3})
    assert upstream.calls[-1] == ("unannotated", {"x": 3})
