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


def test_carry_over_is_caught_across_the_proxy():
    ledger = Session()
    upstream, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})  # returns 1250 cfs
    result = proxy.call_tool("runoff_depth", {"discharge": 1250, "area": 29000})
    assert result["isError"] is True
    assert "no conversion was applied" in result["content"][0]["text"]
    assert len(upstream.calls) == 1


def test_the_same_value_sent_with_its_unit_is_accepted():
    ledger = Session()
    upstream, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})
    result = proxy.call_tool(
        "runoff_depth", {"discharge": {"value": 1250, "unit": "cfs"}, "area": 29000})
    assert not result.get("isError")
    assert upstream.calls[-1][1]["discharge"] == pytest.approx(35.396, rel=1e-3)


def test_carry_over_reports_the_same_code_as_the_decorator():
    """The proxy raised a bare GuardViolation, so the same failure had two codes."""
    ledger = Session()
    upstream, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})
    result = proxy.call_tool("runoff_depth", {"discharge": 1250, "area": 29000})
    assert "[unconverted_carry_over]" in result["content"][0]["text"]


def test_a_new_client_session_does_not_inherit_the_previous_one_s_ledger():
    """One ledger for the process lifetime leaked between conversations.

    A legitimate call failed because an unrelated earlier conversation had returned the
    same number, and the error text quoted that conversation's tool output back to the
    current caller.
    """
    ledger = Session()
    upstream, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})
    assert proxy.call_tool(
        "runoff_depth", {"discharge": 1250, "area": 29000})["isError"] is True

    proxy.begin_session()

    # A different conversation, which never called read_discharge.
    result = proxy.call_tool("runoff_depth", {"discharge": 1250, "area": 29000})
    assert not result.get("isError"), result


def test_the_ledger_is_bounded_when_a_limit_is_set():
    ledger = Session(max_entries=8)
    upstream, proxy = make(ledger)
    for _ in range(50):
        proxy.call_tool("read_discharge", {})
    assert len(proxy.ledger.entries) == 8


def test_guarded_calls_are_counted():
    ledger = Session()
    upstream, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})
    proxy.call_tool("unannotated", {"x": 1})  # not guarded, not counted
    proxy.call_tool("read_discharge", {})
    assert proxy.ledger.calls == 2
    assert "across 2 tool calls" in proxy.ledger.enforcement_report()


def test_a_declared_return_unit_is_advertised_to_the_model():
    """The unit was used to label results and never shown in the schema.

    An opaque abbreviation is exactly the case where the model needs the declaration:
    reading "cfs" it has to already know the expansion.
    """
    _, proxy = make()
    tools = {t["name"]: t for t in proxy.list_tools()}
    assert tools["read_discharge"]["outputSchema"]["x-unit"] == "cfs"
    assert "Returns a value in cfs." in tools["read_discharge"]["description"]
    # The upstream's own wording is kept.
    assert "Latest discharge." in tools["read_discharge"]["description"]
    assert "outputSchema" not in tools["unannotated"]


# Transport ------------------------------------------------------------------------------


def test_serve_stdio_answers_the_protocol(tmp_path):
    import io

    _, proxy = make()
    requests = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "read_discharge", "arguments": {}}},
    ])
    out = io.StringIO()
    serve_stdio(proxy, stdin=io.StringIO(requests), stdout=out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "quantity-guard"
    assert len(replies[1]["result"]["tools"]) == 3
    assert json.loads(replies[2]["result"]["content"][0]["text"])["unit"] == "cfs"


def test_an_unsupported_method_is_method_not_found_not_a_server_fault():
    """A client probing for an optional capability should see -32601."""
    import io

    _, proxy = make()
    requests = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
    ])
    out = io.StringIO()
    serve_stdio(proxy, stdin=io.StringIO(requests), stdout=out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[1]["error"]["code"] == -32601
    assert "resources/list" in replies[1]["error"]["message"]


def test_initialize_over_the_transport_resets_the_ledger():
    import io

    ledger = Session()
    _, proxy = make(ledger)
    proxy.call_tool("read_discharge", {})
    assert proxy.ledger.entries

    out = io.StringIO()
    serve_stdio(proxy,
                stdin=io.StringIO(json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})),
                stdout=out)
    assert proxy.ledger.entries == []


CHILD_SERVER = '''
import json, sys
TOOLS = [{"name": "read_discharge", "description": "d",
          "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    if m.get("id") is None:
        continue
    method = m["method"]
    if method == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {},
             "serverInfo": {"name": "child"}}
    elif method == "tools/list":
        r = {"tools": TOOLS}
    else:
        r = {"content": [{"type": "text", "text": "1250.0"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def test_stdio_upstream_drives_a_real_child_process(tmp_path):
    script = tmp_path / "child_server.py"
    script.write_text(CHILD_SERVER)
    upstream = StdioUpstream([sys.executable, str(script)])
    try:
        assert [t["name"] for t in upstream.list_tools()] == ["read_discharge"]
        with session() as ledger:
            proxy = GuardedProxy(upstream, NOTES, ledger)
            result = proxy.call_tool("read_discharge", {})
            assert json.loads(result["content"][0]["text"])["unit"] == "cfs"
            assert ledger.outputs[-1].quantity.magnitude == pytest.approx(1250.0)
    finally:
        upstream.close()
