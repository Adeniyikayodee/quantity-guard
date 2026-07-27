"""Series-valued quantities.

A gage record is a series, not a number, so the same declarations have to hold when the
magnitude is an array. pint carries the unit maths; what is checked here is that the
reference metadata, the specs, and the ledger behave the same way.
"""

import pytest

np = pytest.importorskip("numpy")

from quantity_guard import Q, Spec, datums, quantity_tool, session

SERIES = [980.0, 1250.0, 1640.0, 1370.0]


def test_a_series_carries_its_unit():
    q = Q(SERIES, "cfs")
    assert not q.is_scalar
    assert q.to("m**3/s").magnitude[1] == pytest.approx(35.396, rel=1e-3)


def test_a_scalar_is_still_a_scalar():
    assert Q(1250.0, "cfs").is_scalar


def test_arithmetic_broadcasts_and_keeps_the_metadata():
    depth = Q(SERIES, "cfs").to("m**3/s") / Q(29000, "km**2")
    assert depth.to("mm/day").magnitude.shape == (4,)
    assert Q(SERIES, "cfs", quality="P").to("m**3/s").quality == "provisional"


def test_quality_propagates_across_a_series():
    total = Q(SERIES, "cfs", quality="approved") + Q(10.0, "cfs", quality="provisional")
    assert total.quality == "provisional"
    assert total.magnitude[0] == pytest.approx(990.0)


def test_a_datum_still_applies_to_a_series():
    datums.register("GAGE:SERIES")
    datums.register_offset("GAGE:SERIES", "NAVD88", Q(1.5, "ft"))
    stage = Q([12.4, 13.0], "ft", datum="GAGE:SERIES")
    shifted = stage.to_datum("NAVD88")
    assert shifted.datum == "NAVD88"
    assert shifted.magnitude[0] == pytest.approx(13.9)


def test_differencing_across_datums_is_refused_for_series_too():
    from quantity_guard import DatumMismatch

    datums.register("GAGE:SERIES2")
    with pytest.raises(DatumMismatch):
        Q([31.0, 31.0], "ft", datum="NAVD88") - Q([12.4, 13.0], "ft", datum="GAGE:SERIES2")


def test_a_bare_series_is_read_in_the_declared_unit():
    """Same contract as a bare number: the schema states the unit, so the list is in it."""
    coerced = Spec(unit="m**3/s").coerce(SERIES, "discharge")
    assert coerced.magnitude[2] == pytest.approx(1640.0)


def test_a_series_sent_with_its_unit_is_converted():
    coerced = Spec(unit="m**3/s").coerce({"value": SERIES, "unit": "cfs"}, "discharge")
    assert coerced.magnitude[2] == pytest.approx(46.44, rel=1e-3)


def test_a_tool_accepts_a_series_and_validates_it():
    from quantity_guard import DimensionalityError

    @quantity_tool(params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
                   returns={"unit": "mm/day"})
    def runoff(discharge, area):
        return (discharge / area).to("mm/day")

    result = runoff({"value": SERIES, "unit": "cfs"}, 29000)
    assert result.magnitude.shape == (4,)
    with pytest.raises(DimensionalityError):
        runoff({"value": SERIES, "unit": "ft"}, 29000)


def test_a_series_formats_as_a_summary():
    assert "4 values" in f"{Q(SERIES, 'cfs')}"


def test_a_series_serialises_as_a_list():
    payload = Q(SERIES, "cfs").as_dict()
    assert payload["value"] == SERIES and payload["unit"] == "cfs"


def test_the_ledger_records_a_series_but_never_matches_against_it():
    """Carry-over compares one magnitude; a series has no single magnitude to compare."""

    @quantity_tool(returns={"unit": "cfs"})
    def read_series():
        return Q(SERIES, "cfs")

    @quantity_tool(params={"q": {"unit": "m**3/s"}}, returns={"unit": "m**3/s"})
    def downstream(q):
        return q

    with session() as ledger:
        read_series()
        assert len(ledger.outputs) == 1
        assert ledger.scalar_outputs == []
        # 1250 appears inside the series, but a series is never a carry-over source.
        assert downstream(1250).magnitude == pytest.approx(1250)
        assert ledger.manifest()["quantities"][0]["value"] == SERIES
