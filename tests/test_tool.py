import pytest

from quantity_guard import (
    DimensionalityError,
    MissingUnit,
    Q,
    QualityViolation,
    TimezoneError,
    quantity_tool,
)


@quantity_tool(
    params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
    returns={"unit": "mm/day"},
)
def runoff_depth(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return discharge / area


def test_bare_number_is_read_in_the_declared_unit():
    assert runoff_depth(35.4, 29000).to("mm/day").magnitude == pytest.approx(0.1054, rel=1e-2)


def test_explicit_units_are_converted():
    from_cfs = runoff_depth("1250 cfs", "29000 km**2")
    from_si = runoff_depth(35.396, 29000)
    assert from_cfs.magnitude == pytest.approx(from_si.magnitude, rel=1e-3)


def test_object_form_is_accepted():
    result = runoff_depth({"value": 1250, "unit": "cfs"}, 29000)
    assert result.magnitude > 0


def test_wrong_dimension_is_refused():
    with pytest.raises(DimensionalityError) as exc:
        runoff_depth("12.4 ft", 29000)
    assert exc.value.field == "discharge"


def test_invoke_returns_a_repairable_tool_error():
    result = runoff_depth.invoke({"discharge": {"value": 12.4, "unit": "ft"}, "area": 29000})
    assert result["isError"] is True
    assert result["code"] == "dimensionality_error"
    assert "no conversion exists" in result["content"][0]["text"]


def test_invoke_serialises_a_successful_result():
    result = runoff_depth.invoke({"discharge": "1250 cfs", "area": 29000})
    assert result["isError"] is False
    assert "value" in result["result"] and "unit" in result["result"]


def test_schema_carries_physical_metadata():
    schema = runoff_depth.json_schema()
    discharge = schema["inputSchema"]["properties"]["discharge"]
    assert discharge["x-unit"] == "m**3/s"
    assert set(schema["inputSchema"]["required"]) == {"discharge", "area"}
    assert any(v.get("type") == "object" for v in discharge["oneOf"])


def test_explicit_unit_may_be_required():
    @quantity_tool(params={"q": {"unit": "m**3/s", "require_explicit_unit": True}})
    def strict(q):
        return q

    with pytest.raises(MissingUnit):
        strict(1250)
    assert strict("1250 cfs").magnitude == pytest.approx(35.4, rel=1e-2)

    schema = strict.json_schema()["inputSchema"]["properties"]["q"]
    assert all(v.get("type") != "number" for v in schema["oneOf"])


def test_quality_floor_is_enforced():
    @quantity_tool(params={"q": {"unit": "m**3/s", "quality": "approved"}})
    def publish(q):
        return q

    with pytest.raises(QualityViolation):
        publish({"value": 1250, "unit": "cfs", "quality": "provisional"})
    assert publish({"value": 1250, "unit": "cfs", "quality": "approved"})


def test_datum_declared_on_a_spec_is_applied_and_checked():
    @quantity_tool(params={"stage": {"unit": "ft", "datum": "NAVD88"}})
    def elevation(stage):
        return stage

    assert elevation(31.0).datum == "NAVD88"
    with pytest.raises(Exception):
        elevation(Q(12.4, "ft", datum="NGVD29"))


def test_naive_timestamps_are_refused():
    @quantity_tool(params={"observed_at": {"tz": "America/Chicago"}})
    def lookup(observed_at):
        return observed_at

    with pytest.raises(TimezoneError) as exc:
        lookup("2026-08-14T09:30:00")
    assert "timezone-naive" in exc.value.message

    aware = lookup("2026-08-14T09:30:00-05:00")
    assert aware.tzinfo is not None


def test_spec_for_unknown_parameter_is_rejected():
    with pytest.raises(ValueError):

        @quantity_tool(params={"nope": {"unit": "m"}})
        def tool(q):
            return q


def test_object_form_serialised_into_a_string_is_accepted():
    """Models frequently JSON-encode the object form into the string variant."""
    import json

    payload = json.dumps({"value": 1250.0, "unit": "cfs", "quality": "provisional"})
    result = runoff_depth(payload, "29000 km**2")
    assert result.to("mm/day").magnitude == pytest.approx(0.1054, rel=1e-2)
