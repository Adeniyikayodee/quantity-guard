"""Retrieval from the UK Environment Agency flood-monitoring service.

Run against a recorded response, so the tests neither need the network nor break when the
river changes. The point of the pack is that the datum hazard is not a US phenomenon: the
Agency publishes levels as mASD, metres above the station's own zero, and relating one to
an absolute elevation needs the offset from the station record.
"""

from pathlib import Path

import pytest

from quantity_guard import DatumMismatch, Q, datums
from quantity_guard.packs import ea

FIXTURES = Path(__file__).parent / "fixtures"
STATION = "E21136"


def fetch(url: str) -> str:
    name = "ea_measures_E21136.json" if "measures" in url else "ea_station_E21136.json"
    return (FIXTURES / name).read_text()


def test_the_station_record_is_read():
    record = ea.station(STATION, fetch=fetch)
    assert record.name == "Hemingford"
    assert record.latitude and record.longitude


def test_reading_the_station_registers_its_datum_against_ordnance_datum():
    record = ea.station(STATION, fetch=fetch)
    assert record.datum_name == f"GAUGE:{STATION}"
    assert record.datum_offset.to("m").magnitude == pytest.approx(6.3)
    assert record.datum_offset.datum == "ODN"
    assert datums.can_convert(record.datum_name, "ODN")


def test_a_stage_comes_back_on_the_station_datum():
    record, values = ea.reading(STATION, fetch=fetch)
    stage = values[0].value
    assert stage.datum == record.datum_name
    assert stage.to("m").magnitude == pytest.approx(0.117)


def test_the_registered_offset_relates_it_to_ordnance_datum():
    _, values = ea.reading(STATION, fetch=fetch)
    assert values[0].value.to_datum("ODN").magnitude == pytest.approx(6.417)


def test_a_stage_cannot_be_differenced_against_an_absolute_elevation():
    _, values = ea.reading(STATION, fetch=fetch)
    with pytest.raises(DatumMismatch):
        Q(9.0, "m", datum="ODN") - values[0].value


def test_timestamps_are_timezone_aware():
    _, values = ea.reading(STATION, fetch=fetch)
    assert values[0].observed_at.tzinfo is not None


# Unit names -------------------------------------------------------------------------------


@pytest.mark.parametrize("name,unit,surface", [
    ("mASD", "meter", "station"),
    ("mAOD", "meter", "ODN"),
    ("m3/s", "meter**3/second", None),
    ("mm", "millimeter", None),
])
def test_service_unit_names_carry_their_surface(name, unit, surface):
    """mASD and mAOD are both metres; only the surface they start from differs."""
    assert ea.unit_for(name) == (unit, surface)


def test_an_unmapped_name_says_so_rather_than_guessing():
    with pytest.raises(ValueError, match="unmapped Environment Agency unit"):
        ea.unit_for("furlongs")


# The registry is not US-only ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["ODN", "NAP", "EVRF2019", "DHHN2016", "AHD",
                                  "CGVD2013", "NZVD2016", "LAT", "MALIN"])
def test_national_datums_are_referenceable_without_registering_them(name):
    assert Q(1.0, "m", datum=name).datum == name


def test_two_national_datums_cannot_be_mixed():
    with pytest.raises(DatumMismatch):
        Q(12.0, "m", datum="ODN") - Q(3.0, "m", datum="NAP")


def test_an_offset_between_national_datums_is_never_assumed():
    from quantity_guard import DatumConversionUnavailable

    with pytest.raises(DatumConversionUnavailable):
        Q(12.0, "m", datum="ODN").to_datum("NAP")


@pytest.mark.parametrize("code,expected", [
    ("Good", "approved"), ("Unchecked", "provisional"),
    ("Estimated", "estimated"), ("Suspect", "unverified"),
])
def test_environment_agency_quality_codes_are_understood(code, expected):
    assert Q(1.0, "m", quality=code).quality == expected


@pytest.mark.live
def test_against_the_live_service():
    record, values = ea.reading(STATION)
    assert record.name
    assert all(v.value.dimensionality == Q(1, "m").dimensionality
               for v in values if v.value.units == Q(1, "m").units)
