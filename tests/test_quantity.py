import pytest

from quantity_guard import (
    CRSMismatch,
    DatumConversionUnavailable,
    DatumMismatch,
    DimensionalityError,
    Q,
    UnitParseError,
    datums,
)


def test_unit_conversion_preserves_metadata():
    q = Q(1250, "cfs", quality="provisional", source="usgs").to("m**3/s")
    assert q.magnitude == pytest.approx(35.396, rel=1e-3)
    assert q.quality == "provisional"
    assert q.source == "usgs"


def test_incompatible_conversion_is_refused():
    with pytest.raises(DimensionalityError):
        Q(12.4, "ft").to("m**3/s")


def test_usgs_quality_codes_are_normalised():
    assert Q(1, "cfs", quality="P").quality == "provisional"
    assert Q(1, "cfs", quality="A").quality == "approved"


def test_quality_propagates_as_the_weakest_input():
    total = Q(1250, "cfs", quality="approved") + Q(90, "cfs", quality="provisional")
    assert total.quality == "provisional"


def test_differencing_a_shared_datum_yields_a_delta():
    freeboard = Q(31.0, "ft", datum="NAVD88") - Q(28.1, "ft", datum="NAVD88")
    assert freeboard.datum is None
    assert freeboard.magnitude == pytest.approx(2.9)


def test_differencing_across_datums_is_refused():
    datums.register("TEST_LOCAL")
    with pytest.raises(DatumMismatch) as exc:
        Q(31.0, "ft", datum="NAVD88") - Q(12.4, "ft", datum="TEST_LOCAL")
    assert "different references" in exc.value.message


def test_adding_two_absolute_elevations_is_refused():
    with pytest.raises(DatumMismatch):
        Q(31.0, "ft", datum="NAVD88") + Q(28.1, "ft", datum="NAVD88")


def test_a_delta_may_be_added_to_an_elevation():
    result = Q(28.1, "ft", datum="NAVD88") + Q(2.9, "ft")
    assert result.datum == "NAVD88"
    assert result.magnitude == pytest.approx(31.0)


@pytest.mark.parametrize("text,unit", [
    # The spelling NWIS publishes as a unit code, and the one models write in prose.
    ("1250 ft3/s", "foot**3/second"),
    ("1250 m3/s", "meter**3/second"),
    ("1250 ft^3/s", "foot**3/second"),
    ("1250 ft**3/s", "foot**3/second"),
    ("150000 acre-ft", "acre*foot"),
    ("1250 cusecs", "foot**3/second"),
    ("35.4 cumecs", "meter**3/second"),
])
def test_service_and_prose_spellings_parse(text, unit):
    assert Q.parse(text).dimensionality == Q(1, unit).dimensionality


def test_an_exponent_in_scientific_notation_is_not_read_as_a_unit_exponent():
    assert Q.parse("1e3 cfs").magnitude == pytest.approx(1000)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_scalar_is_refused(value):
    """Left alone it fails every comparison and surfaces as unsourced, not as invalid."""
    with pytest.raises(UnitParseError):
        Q(value, "m**3/s")


def test_a_series_keeps_its_gaps_and_still_serialises_as_valid_json():
    numpy = pytest.importorskip("numpy")
    import json

    q = Q(numpy.array([1.0, float("nan"), 3.0]), "ft")
    assert json.loads(json.dumps(q.as_dict()))["value"] == [1.0, None, 3.0]
    assert "1 missing" in repr(q)


@pytest.mark.parametrize("op", [
    lambda a, b: a + b,
    lambda a, b: a * b,
])
def test_degree_scale_arithmetic_is_a_guard_violation_not_a_pint_error(op):
    """Water temperature maps onto degC, so this path is reachable from the packs.

    A raw pint error carries no repair() text and escapes the tool-error path.
    """
    with pytest.raises(DimensionalityError) as exc:
        op(Q(21.0, "degC"), Q(1.0, "degC"))
    assert "point on that scale" in exc.value.message


def test_scaling_a_degree_scale_by_a_number_is_also_refused():
    with pytest.raises(DimensionalityError):
        Q(21.0, "degC") * 2
    # Differencing two temperatures is an interval, and stays legal.
    assert (Q(21.0, "degC") - Q(18.0, "degC")).magnitude == pytest.approx(3.0)
    # Kelvin is an absolute scale and scales normally.
    assert (Q(294.0, "K") * 2).magnitude == pytest.approx(588.0)


@pytest.mark.parametrize("left,right", [("VA", "W"), ("VA", "var"), ("var", "W")])
def test_real_apparent_and_reactive_power_do_not_interconvert(left, right):
    """Converting between them needs a power factor, a property of the circuit.

    Defining VA and var as `volt * ampere` made them silent aliases of the watt and of
    each other, so 100 VA became 100 W without complaint.
    """
    with pytest.raises(DimensionalityError):
        Q(100.0, left).to(right)


@pytest.mark.parametrize("prefixed,base", [("MVA", "VA"), ("kvar", "var"), ("MW", "W")])
def test_prefixes_still_work_on_the_power_units(prefixed, base):
    assert Q(1.0, prefixed).to(base).magnitude > 1


def test_the_two_million_gallon_per_day_units_are_named_apart():
    """UK practice writes mgd for imperial gallons; the difference is 20%."""
    assert Q(1.0, "us_mgd").to("megaliter/day").magnitude == pytest.approx(3.78541, rel=1e-5)
    assert Q(1.0, "imperial_mgd").to("megaliter/day").magnitude == pytest.approx(4.54609, rel=1e-5)


def test_scaling_an_elevation_cannot_launder_its_datum():
    """`elevation * 1` must not return a datum-free delta.

    The scaled value would otherwise pass every downstream check, which reproduces the
    exact freeboard error the datum guard exists to prevent.
    """
    crest = Q(31.0, "ft", datum="NGVD29")
    surface = Q(26.5, "ft", datum="NAVD88")
    for launder in (lambda q: q * 1, lambda q: q / 1, lambda q: 1 * q):
        with pytest.raises(DatumMismatch):
            launder(crest)
        with pytest.raises(DatumMismatch):
            launder(crest) - surface


def test_scaling_a_delta_is_still_allowed():
    assert (Q(5.0, "ft") * 2).magnitude == pytest.approx(10.0)
    assert (Q(5.0, "ft") / 2).magnitude == pytest.approx(2.5)


def test_subtracting_an_elevation_from_a_delta_is_refused():
    """`delta - elevation` is neither an elevation nor a delta."""
    with pytest.raises(DatumMismatch) as exc:
        Q(5.0, "ft") - Q(31.0, "ft", datum="NAVD88")
    assert "neither an elevation nor a delta" in exc.value.message


def test_a_delta_may_be_subtracted_from_an_elevation():
    result = Q(31.0, "ft", datum="NAVD88") - Q(2.9, "ft")
    assert result.datum == "NAVD88"
    assert result.magnitude == pytest.approx(28.1)


def test_datum_shift_requires_a_registered_offset():
    with pytest.raises(DatumConversionUnavailable):
        Q(31.0, "ft", datum="NAVD88").to_datum("NGVD29")


def test_registered_offset_enables_the_shift():
    datums.register("TEST_GAGE")
    datums.register_offset("TEST_GAGE", "NAVD88", Q(1.5, "ft"))
    shifted = Q(12.4, "ft", datum="TEST_GAGE").to_datum("NAVD88")
    assert shifted.magnitude == pytest.approx(13.9)
    assert shifted.datum == "NAVD88"


def test_comparison_across_datums_is_refused():
    datums.register("TEST_CMP")
    with pytest.raises(DatumMismatch):
        assert Q(31.0, "ft", datum="NAVD88") > Q(12.4, "ft", datum="TEST_CMP")


def test_comparison_converts_units():
    assert Q(1, "m") > Q(3, "ft")


def test_multiplying_elevations_is_refused():
    with pytest.raises(DatumMismatch):
        Q(31.0, "ft", datum="NAVD88") * Q(2.0, "ft", datum="NAVD88")


def test_division_carries_units_through():
    depth = Q(35.4, "m**3/s") / Q(29000, "km**2")
    assert depth.to("mm/day").magnitude == pytest.approx(0.1054, rel=1e-2)


def test_parse_from_string():
    q = Q.parse("1250 cfs")
    assert q.magnitude == 1250


def test_bare_string_without_unit_is_refused():
    with pytest.raises(Exception):
        Q.parse("1250")


# Equality, and the frames it has to respect ---------------------------------------------


def test_equality_converts_as_the_ordering_operators_do():
    """A dataclass compares field by field, which disagreed with `<` and `>` beside it.

    `Q(1, "m")` was neither less than, greater than, nor equal to `Q(100, "cm")`, which
    is not a consistent ordering by any reading.
    """
    assert Q(1, "m") == Q(100, "cm")
    assert not Q(1, "m") != Q(100, "cm")
    assert (Q(1, "m") <= Q(100, "cm"), Q(1, "m") >= Q(100, "cm")) == (True, True)
    assert hash(Q(1, "m")) == hash(Q(100, "cm"))


def test_equality_ignores_provenance_but_not_the_frame():
    assert Q(3, "ft", quality="provisional", source="a") == Q(3, "ft", quality="approved")
    assert Q(1, "m") != Q(1, "s")  # different dimensions are unequal, not an error


def test_equality_refuses_a_cross_datum_comparison():
    """`False` is an answer a caller acts on, and `<` already refuses this."""
    with pytest.raises(DatumMismatch):
        Q(12.4, "ft", datum="NAVD88") == Q(12.4, "ft")
    with pytest.raises(DatumMismatch):
        Q(12.4, "ft", datum="NAVD88") == Q(12.4, "ft", datum="NGVD29")


def test_a_series_compares_without_ambiguity():
    """Field-by-field equality on an array raised rather than answering."""
    numpy = pytest.importorskip("numpy")
    assert Q([1.0, 2.0], "m") == Q([100.0, 200.0], "cm")
    assert Q([1.0, 2.0], "m") != Q([1.0, 3.0], "m")
    # A gap compares equal to a gap: one series with one sample missing is one series.
    assert Q([1.0, numpy.nan], "m") == Q([1.0, numpy.nan], "m")
    with pytest.raises(TypeError):
        hash(Q([1.0, 2.0], "m"))


def test_the_coordinate_frame_is_checked_on_every_combining_operation():
    """Products and comparisons were unchecked, so the result took one frame silently."""
    here, there = Q(10, "m", crs="EPSG:4326"), Q(2, "m", crs="EPSG:26915")
    for operation in (lambda: here * there, lambda: here / there,
                      lambda: here + there, lambda: here - there,
                      lambda: here < there, lambda: here == there):
        with pytest.raises(CRSMismatch):
            operation()
    # One frame stated and the other left open is still a legal combination.
    assert (here * Q(2, "m")).crs == "EPSG:4326"
