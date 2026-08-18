"""Provider-shaped tool definitions, and the warn-mode report."""

import json

import pytest

from quantity_guard import (
    CRSMismatch,
    DimensionalityError,
    GuardedTool,
    Q,
    quantity_tool,
    session,
)
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


def _wrong_return_tool(enforcement, returns={"unit": "m**3/s"}):
    def leaky(q):
        """Declares a discharge and returns a length."""
        return Q(3.0, "ft")

    return GuardedTool(leaky, {"q": {"unit": "m**3/s"}}, returns, enforcement=enforcement)


def test_warn_mode_does_not_reject_on_the_return_path():
    """A violation on the way out is recorded and passed through, as on the way in.

    Returns were previously validated unconditionally, so warn mode raised from the one
    place it promises not to, and the violation went unrecorded.
    """
    tool = _wrong_return_tool("warn")
    with session() as ledger:
        assert tool(5.0) == Q(3.0, "ft")
    assert [v.field for v in ledger.violations] == ["return"]
    assert ledger.violations[0].code == "dimensionality_error"
    assert "1 of 1 tool calls would have been blocked" in ledger.enforcement_report()


def test_warn_mode_tolerates_a_return_of_the_wrong_shape():
    tool = _wrong_return_tool("warn", returns={"depth": {"unit": "mm/day"}})
    with session() as ledger:
        assert tool(5.0) == Q(3.0, "ft")
    assert [v.code for v in ledger.violations] == ["guard_violation"]


def test_strict_mode_still_rejects_on_the_return_path():
    with session():
        with pytest.raises(DimensionalityError):
            _wrong_return_tool("strict")(5.0)


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


def test_the_schema_uses_anyof_rather_than_oneof():
    """The variants are disjoint by type, so the two mean the same thing here, and
    `oneOf` is the one OpenAI's structured outputs refuse."""
    prop = schema(_runoff_tool(), "openai")["function"]["parameters"]["properties"]["discharge"]
    assert "anyOf" in prop and "oneOf" not in prop


def test_strict_mode_emits_a_schema_openai_will_accept():
    """Strict mode validates the schema itself: no unrecognised keyword, and every
    property of every object listed as required."""
    definition = schema(_runoff_tool(), "openai", strict=True)
    assert definition["function"]["strict"] is True
    parameters = definition["function"]["parameters"]
    assert "x-" not in json.dumps(parameters)

    def every_object(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                yield node
            for value in node.values():
                yield from every_object(value)
        elif isinstance(node, list):
            for value in node:
                yield from every_object(value)

    for obj in every_object(parameters):
        assert set(obj["required"]) == set(obj["properties"])
        assert obj["additionalProperties"] is False

    # The declaration is not lost, only moved to where a restricted dialect can carry it.
    assert "In m**3/s." in parameters["properties"]["discharge"]["description"]


def test_strict_is_refused_where_it_would_do_nothing():
    with pytest.raises(ValueError, match="OpenAI"):
        schema(_runoff_tool(), "anthropic", strict=True)


def _runoff_tool():
    @quantity_tool(
        params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
        returns={"unit": "mm/day"},
    )
    def runoff(discharge, area=None):
        """Depth-equivalent runoff."""
        return discharge

    return runoff
