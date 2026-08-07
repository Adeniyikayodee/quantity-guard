"""Retrieval from USGS Water Services.

Run against responses recorded from the live service, so the tests neither need the
network nor break when the river changes.
"""

from datetime import datetime
from pathlib import Path

import pytest

from quantity_guard import DatumMismatch, Q, datums
from quantity_guard.packs import usgs

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "07374000"


def fetch(url: str) -> str:
    name = "usgs_site_07374000.rdb" if "/site/" in url else "usgs_iv_07374000.json"
    return (FIXTURES / name).read_text()


# Site record ------------------------------------------------------------------------------


def test_the_site_record_is_read():
    record = usgs.site(SITE, fetch=fetch)
    assert record.name == "Mississippi River at Baton Rouge, LA"
    assert record.timezone == "CST"
    assert record.horizontal_crs == "NAD83"


def test_the_drainage_area_arrives_as_a_quantity():
    area = usgs.site(SITE, fetch=fetch).drainage_area
    assert area.to("mile**2").magnitude == pytest.approx(1125810, rel=1e-6)


def test_reading_the_site_registers_its_datum():
    record = usgs.site(SITE, fetch=fetch)
    assert record.datum_name == f"GAGE:{SITE}"
    assert record.datum_name in datums.datums
    assert datums.can_convert(record.datum_name, "NAVD88")


# Values -----------------------------------------------------------------------------------


def test_discharge_carries_its_unit_and_qualifier():
    values = usgs.instantaneous(SITE, fetch=fetch)
    discharge = values["00060"].value
    assert discharge.to("cfs").magnitude == pytest.approx(234000)
    # The service marks the reading provisional; that has to survive into the quantity.
    assert discharge.quality == "provisional"
    assert discharge.source == f"usgs:{SITE}:00060"


def test_the_timestamp_keeps_the_offset_the_service_stamped():
    """August readings carry -05:00 even though the site's zone code says CST."""
    observed = usgs.instantaneous(SITE, fetch=fetch)["00060"].observed_at
    assert observed.tzinfo is not None
    assert observed.utcoffset().total_seconds() == -5 * 3600


def test_gage_height_is_returned_on_the_station_datum():
    record, values = usgs.reading(SITE, fetch=fetch)
    stage = values["00065"].value
    assert stage.datum == record.datum_name
    assert stage.to("ft").magnitude == pytest.approx(7.73)


def test_a_stage_cannot_be_differenced_against_an_absolute_elevation():
    _, values = usgs.reading(SITE, fetch=fetch)
    with pytest.raises(DatumMismatch):
        Q(31.0, "ft", datum="NAVD88") - values["00065"].value


def test_the_registered_offset_makes_the_comparison_legal():
    _, values = usgs.reading(SITE, fetch=fetch)
    stage = values["00065"].value.to_datum("NAVD88")
    assert (Q(31.0, "ft", datum="NAVD88") - stage).magnitude == pytest.approx(23.27)


# Unit codes -------------------------------------------------------------------------------


def test_service_unit_codes_map_to_pint():
    assert usgs.unit_for("ft3/s") == "foot**3/second"
    assert Q(1.0, usgs.unit_for("ft3/s")).to("cfs").magnitude == pytest.approx(1.0)


def test_an_unmapped_code_says_so_rather_than_guessing():
    with pytest.raises(ValueError, match="unmapped USGS unit code"):
        usgs.unit_for("furlongs/fortnight")


@pytest.mark.parametrize("qualifiers,expected", [
    (["P"], "provisional"), (["A"], "approved"), (["A", "e"], "estimated"), ([], None),
])
def test_qualifiers_become_quality_flags(qualifiers, expected):
    assert Q(1.0, "ft", quality=usgs._quality(qualifiers)).quality == expected


# Live service -----------------------------------------------------------------------------


@pytest.mark.live
def test_against_the_live_service():
    """Run with -m live to check the recorded fixtures still match the real shape."""
    record, values = usgs.reading(SITE)
    assert record.name
    assert values["00060"].value.dimensionality == Q(1, "cfs").dimensionality
