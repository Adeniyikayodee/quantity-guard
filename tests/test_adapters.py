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


def test_an_unknown_tool_reports_rather_than_raising():
    assert toolbox([runoff]).invoke("nope", {})["isError"] is True


# warn mode --------------------------------------------------------------------------------


def _warn_tool():
    return GuardedTool(
        runoff.fn,
        {"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
        {"unit": "mm/day"},
        enforcement="warn",
    )


def test_warn_mode_reports_what_enforcement_would_have_blocked():
    tool = _warn_tool()
    with session() as ledger:
        tool("12.4 ft", 29000)
        tool("3 kg", 29000)
        tool("1250 cfs", 29000)
        report = ledger.enforcement_report()
    assert "2 of 3 tool calls would have been blocked" in report
    assert "2x dimensionality_error" in report
    assert len(ledger.violations) == 2
    assert ledger.violations[0].tool == "runoff"


def test_a_clean_session_says_so():
    tool = _warn_tool()
    with session() as ledger:
        tool("1250 cfs", 29000)
        assert "No calls would have been blocked" in ledger.enforcement_report()


def test_warn_mode_still_returns_an_answer():
    """The point of warn mode is that nothing breaks while you measure."""
    tool = _warn_tool()
    with session():
        assert tool("12.4 ft", 29000) is not None


# CRS ---------------------------------------------------------------------------------------


def test_a_crs_is_a_consistency_tag_on_a_scalar():
    """A scalar has no coordinates to reproject, so the CRS is checked, never converted."""
    a = Q(3.0, "m", crs="EPSG:4326")
    assert (a + Q(1.0, "m", crs="EPSG:4326")).crs == "EPSG:4326"
    with pytest.raises(CRSMismatch):
        a + Q(1.0, "m", crs="EPSG:26915")


def test_a_percentage_answer_is_a_quantity():
    """A question asking for a percentage is answered dimensionlessly, not without units."""
    from quantity_guard import Q, ureg

    assert ureg.Quantity("20.03 %").to("percent").magnitude == pytest.approx(20.03)
    assert Q(1.0, "m**3/s") / Q(5.0, "m**3/s") == Q(0.2, "dimensionless")
