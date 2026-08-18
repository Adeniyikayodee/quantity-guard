import pytest

from quantity_guard import (
    DimensionalityError,
    MissingUnit,
    Q,
    QualityViolation,
    Spec,
    TimezoneError,
    quantity_tool,
    session,
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
    assert any(v.get("type") == "object" for v in discharge["anyOf"])


def test_explicit_unit_may_be_required():
    @quantity_tool(params={"q": {"unit": "m**3/s", "require_explicit_unit": True}})
    def strict(q):
        return q

    with pytest.raises(MissingUnit):
        strict(1250)
    assert strict("1250 cfs").magnitude == pytest.approx(35.4, rel=1e-2)

    schema = strict.json_schema()["inputSchema"]["properties"]["q"]
    assert all(v.get("type") != "number" for v in schema["anyOf"])


def test_explicit_unit_is_required_on_every_input_shape():
    """The check previously guarded only the bare-number branch.

    A model could satisfy the parameter with an object or a series carrying no unit,
    which is the same omission the declaration exists to refuse.
    """

    @quantity_tool(params={"q": {"unit": "m**3/s", "require_explicit_unit": True}})
    def strict(q):
        return q

    for evasion in ({"value": 1250}, {"value": 1250, "unit": ""},
                    {"value": 1250, "unit": None}, [1250, 1300], (1250, 1300)):
        with pytest.raises(MissingUnit):
            strict(evasion)

    # The forms that do carry a unit are untouched.
    assert strict({"value": 1250, "unit": "cfs"}).magnitude == pytest.approx(35.4, rel=1e-2)


def test_a_unitless_object_is_still_accepted_when_no_unit_is_required():
    @quantity_tool(params={"q": {"unit": "m**3/s"}})
    def lenient(q):
        return q

    assert lenient({"value": 1250}).magnitude == pytest.approx(1250)
    assert lenient([1250, 1300]).units == lenient(1250).units


def test_the_object_variant_declares_only_the_keys_it_enforces():
    """`required` must describe what the validator does, in both directions."""

    def object_variant(spec):
        prop = spec.json_schema()
        return next(v for v in prop["anyOf"] if v.get("type") == "object")

    assert object_variant(Spec(unit="m**3/s", require_explicit_unit=True))["required"] == [
        "value", "unit"]
    assert object_variant(Spec(unit="m**3/s"))["required"] == ["value"]


@pytest.mark.parametrize("returns,is_mapping", [
    # Natural result names for a hydrology tool that collide with Spec's own fields.
    ({"stage": {"unit": "ft"}, "quality": {"unit": None}}, True),
    ({"stage": {"unit": "ft"}, "datum": {"unit": None}}, True),
    ({"depth": {"unit": "mm/day"}}, True),
    # A single declaration: every key is a Spec field.
    ({"unit": "ft"}, False),
    ({"unit": "ft", "quality": "approved"}, False),
])
def test_a_returns_mapping_may_use_names_that_are_also_spec_fields(returns, is_mapping):
    """Membership was decided by intersection, so one colliding key broke the whole thing."""

    @quantity_tool(params={"s": {"unit": "ft"}}, returns=returns)
    def measure(s):
        """A reading."""
        return s

    assert isinstance(measure.returns, dict) is is_mapping


def test_quality_floor_is_enforced():
    @quantity_tool(params={"q": {"unit": "m**3/s", "quality": "approved"}})
    def publish(q):
        return q

    with pytest.raises(QualityViolation):
        publish({"value": 1250, "unit": "cfs", "quality": "provisional"})
    assert publish({"value": 1250, "unit": "cfs", "quality": "approved"})


def test_an_unflagged_record_cannot_satisfy_a_quality_floor():
    """Absence of a qualifier is not evidence of approval.

    The check previously ran only when the value happened to carry a flag, so dropping
    the flag satisfied the requirement.
    """

    @quantity_tool(params={"q": {"unit": "m**3/s", "quality": "approved"}})
    def publish(q):
        return q

    for unflagged in (1250, {"value": 1250, "unit": "cfs"}, "1250 cfs", Q(1250, "cfs")):
        with pytest.raises(QualityViolation):
            publish(unflagged)


def test_a_tool_with_no_quality_floor_still_takes_unflagged_record():
    @quantity_tool(params={"q": {"unit": "m**3/s"}})
    def anything(q):
        return q

    assert anything(1250).quality is None


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


def test_a_string_that_is_not_a_quantity_still_fails():
    with pytest.raises(Exception):
        runoff_depth("{not json at all}", 29000)


# What the declaration promises, and what the model is told ------------------------------


def test_an_optional_quantity_parameter_may_be_omitted():
    """`apply_defaults` filled the parameter in before validation, so its own default
    was validated as though the caller had sent it, and every optional quantity
    parameter raised on the calls that left it out."""

    @quantity_tool(params={"flow": {"unit": "m**3/s"}, "area": {"unit": "km**2"}})
    def optional(flow, area=None):
        return flow if area is None else flow / area

    assert optional(Q(10, "m**3/s")) == Q(10, "m**3/s")
    assert optional(Q(10, "m**3/s"), area=None) == Q(10, "m**3/s")
    assert optional.invoke({"flow": 10})["isError"] is False
    # A default that is a real value is still validated, and reaches the body as one.
    @quantity_tool(params={"flow": {"unit": "m**3/s"}, "area": {"unit": "km**2"}})
    def defaulted(flow, area=100):
        return area

    assert defaulted(Q(10, "m**3/s")) == Q(100, "km**2")


def test_warn_mode_never_raises():
    """`warn` exists to measure what enforcement would block without paying for it.

    The lenient reading was built eagerly, so a value that could not be made into a `Q`
    at all — a non-finite magnitude — raised from the one mode that promises not to.
    """

    @quantity_tool(params={"flow": {"unit": "m**3/s"}}, enforcement="warn")
    def lenient(flow):
        return flow

    with session() as ledger:
        assert lenient(float("nan")) != lenient(float("nan"))  # the raw float, passed on
    assert [v.code for v in ledger.violations] == ["unit_parse_error"] * 2


def test_every_violation_names_the_parameter_it_is_about():
    """Repair text is the point of these errors, and a violation raised inside `Q` knew
    what was wrong but not which of the tool's arguments carried it."""

    @quantity_tool(params={"flow": {"unit": "m**3/s"}, "depth": {"unit": "mm"}})
    def two(flow, depth):
        return flow

    for payload, code in (
        ({"flow": "1250", "depth": 1}, "unit_parse_error"),
        ({"flow": 1, "depth": {"value": 1, "unit": "ft", "datum": "NOPE"}}, "datum_mismatch"),
    ):
        error = two.invoke(payload)
        assert error["code"] == code
        assert error["field"] in ("flow", "depth")
        assert f"`{error['field']}`" in error["content"][0]["text"]


def test_an_argument_the_tool_does_not_take_is_a_violation_like_any_other():
    error = runoff_depth.invoke({"discharge": 1, "area": 1, "rainfall": 2})
    assert error["code"] == "invalid_arguments"
    assert "rainfall" in error["content"][0]["text"]


def test_a_spec_declares_a_quantity_or_a_timestamp_but_not_both():
    """`is_temporal` reads a spec as a timestamp only when it declares no unit, so a
    spec carrying both silently dropped the timezone: unchecked, and absent from the
    schema."""
    with pytest.raises(ValueError, match="not both"):
        Spec(unit="ft", tz="America/Chicago")


def test_the_object_variant_declares_every_key_the_validator_reads():
    """`additionalProperties: false` beside a validator that accepts more than it
    advertises made a model reporting a retrieved datum send a schema-invalid call."""
    prop = Spec(unit="ft").json_schema()
    variant = next(v for v in prop["anyOf"] if v.get("type") == "object")
    assert set(variant["properties"]) == {"value", "unit", "datum", "crs", "quality", "source"}
    # A series is accepted at the boundary, so it is advertised at the boundary too.
    assert any(v.get("type") == "array" for v in prop["anyOf"])
    assert any(v.get("type") == "array" for v in variant["properties"]["value"]["anyOf"])


def test_a_result_schema_is_an_object_schema():
    """MCP's outputSchema describes structuredContent, which is an object."""
    schema = Spec(unit="ft", datum="NAVD88").result_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["value", "unit"]
    assert schema["x-datum"] == "NAVD88"
