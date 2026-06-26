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
