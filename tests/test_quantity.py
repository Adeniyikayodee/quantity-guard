import pytest

from quantity_guard import (
    DatumConversionUnavailable,
    DatumMismatch,
    DimensionalityError,
    Q,
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
