"""Provider-shaped tool definitions, and the warn-mode report."""

import json

import pytest

from quantity_guard import CRSMismatch, GuardedTool, Q, quantity_tool, session
from quantity_guard.adapters import Toolbox, schema, toolbox


@quantity_tool(
    params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
    returns={"unit": "mm/day"},
)
def runoff(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return (discharge / area).to("mm/day")


def test_openai_shape():
    fn = schema(runoff, "openai")["function"]
    assert fn["name"] == "runoff"
    assert fn["parameters"]["properties"]["discharge"]["x-unit"] == "m**3/s"


def test_anthropic_shape():
    tool = schema(runoff, "anthropic")
    assert set(tool) == {"name", "description", "input_schema"}
    assert tool["input_schema"]["properties"]["area"]["x-unit"] == "km**2"


def test_mcp_shape_is_the_default():
    assert "inputSchema" in schema(runoff)


def test_an_unknown_flavour_is_refused():
    with pytest.raises(ValueError):
        schema(runoff, "gemini")


def test_the_plain_schema_drops_the_metadata():
    fn = schema(runoff, "openai", physical_metadata=False)["function"]
    assert "x-unit" not in fn["parameters"]["properties"]["discharge"]


def test_toolbox_dispatches_and_wraps_the_result():
    box = toolbox([runoff])
    payload = box.invoke("runoff", {"discharge": "1250 cfs", "area": 29000})
    assert not payload["isError"]

    openai = box.result_message("openai", "call_1", payload)
    assert openai["role"] == "tool"
    assert json.loads(openai["content"])["unit"] == "mm / d"

    anthropic = box.result_message("anthropic", "toolu_1", payload)
    assert anthropic["type"] == "tool_result" and anthropic["is_error"] is False


def test_a_rejected_call_becomes_an_error_result_not_an_exception():
    box = toolbox([runoff])
    payload = box.invoke("runoff", {"discharge": "12.4 ft", "area": 29000})
    message = box.result_message("anthropic", "toolu_2", payload)
    assert message["is_error"] is True
    assert "no conversion exists" in message["content"][0]["text"]
