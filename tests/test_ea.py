"""Retrieval from the UK Environment Agency flood-monitoring service.

Run against a recorded response, so the tests neither need the network nor break when the
river changes. The point of the pack is that the datum hazard is not a US phenomenon: the
Agency publishes levels as mASD, metres above the station's own zero, and relating one to
an absolute elevation needs the offset from the station record.
"""

from pathlib import Path

import pytest

from quantity_guard import DatumConversionUnavailable, DatumMismatch, Q, datums
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


def test_a_station_with_no_published_offset_is_still_readable():
    """Most EA stations publish no datumOffset.

    Registration of the station's own datum was previously conditional on the offset, so
    `ea.reading` raised `DatumMismatch: unknown datum 'GAUGE:...'` for the majority of the
    network. The frame exists whether or not its height above ODN is published; only the
    conversion depends on the offset.
    """
    import json

    record = json.loads((FIXTURES / "ea_station_E21136.json").read_text())
    record["items"].pop("datumOffset")

    def without_offset(url):
        if "measures" in url:
            return (FIXTURES / "ea_measures_E21136.json").read_text()
        return json.dumps(record)

    station, values = ea.reading("X9999", fetch=without_offset)
    assert station.datum_offset is None
    assert values[0].value.datum == "GAUGE:X9999"

    # Labelled, so it cannot be differenced against an absolute elevation by accident,
    # but not convertible, because the offset was never published.
    with pytest.raises(DatumConversionUnavailable):
        values[0].value.to_datum("ODN")


@pytest.mark.parametrize("reference,expected", [
    ("E21136", "E21136"),
    # The live station list carries references with spaces. The service spells them with
    # an underscore in its own @id and answers only to that form; percent-encoding the
    # space returns 500 and a raw space raises InvalidURL at the client.
    ("055003_TG 316", "055003_TG_316"),
    ("067027_TG 127", "067027_TG_127"),
    ("  E21136  ", "E21136"),
])
def test_station_references_are_spelled_as_the_service_spells_them(reference, expected):
    seen = []

    def _fetch(url):
        seen.append(url)
        if "measures" in url:
            return (FIXTURES / "ea_measures_E21136.json").read_text()
        return (FIXTURES / "ea_station_E21136.json").read_text()

    ea.reading(reference, fetch=_fetch)
    assert all(f"/id/stations/{expected}" in url for url in seen), seen


@pytest.mark.parametrize("name", ["mASD", "mAOD", "m", "mm", "m3/s", "l/s", "Ml/d",
                                  "---", "V", "m/s", "deg", "%", "C", "hPa", "ug/l"])
def test_unit_names_the_live_service_publishes_are_mapped(name):
    assert ea.unit_for(name)[0]


def test_metres_below_datum_is_refused_rather_than_mislabelled():
    """mBDAT measures downward; mapping it to mASD would invert every reading's sign."""
    with pytest.raises(ValueError):
        ea.unit_for("mBDAT")


def test_a_stale_reading_is_dropped_when_a_max_age_is_given():
    """A station with a failed sensor keeps serving its last good value indefinitely."""
    import json
    from datetime import timedelta

    payload = json.loads((FIXTURES / "ea_measures_E21136.json").read_text())
    payload["items"][0]["latestReading"]["dateTime"] = "2019-03-02T06:00:00Z"

    def _fetch(url):
        if "measures" in url:
            return json.dumps(payload)
        return (FIXTURES / "ea_station_E21136.json").read_text()

    assert ea.readings(STATION, fetch=_fetch)[0].age > timedelta(days=1000)

    with pytest.warns(UserWarning, match="dropping EA measure"):
        assert ea.readings(STATION, fetch=_fetch, max_age=timedelta(hours=6)) == []


def test_an_unmapped_measure_does_not_cost_the_whole_station():
    import json

    payload = json.loads((FIXTURES / "ea_measures_E21136.json").read_text())
    exotic = json.loads(json.dumps(payload["items"][0]))
    exotic["unitName"] = "furlongs"
    payload["items"].append(exotic)

    def _fetch(url):
        if "measures" in url:
            return json.dumps(payload)
        return (FIXTURES / "ea_station_E21136.json").read_text()

    with pytest.warns(UserWarning, match="skipping EA measure"):
        values = ea.readings(STATION, fetch=_fetch)
    assert len(values) == 1
    assert values[0].value.magnitude == pytest.approx(0.117)


def test_a_published_quality_word_reaches_the_alias_table():
    """The pack previously read `qualityControl`, a key the service does not publish.

    EA quality was therefore always None and the Good/Unchecked/Estimated/Suspect entries
    in QUALITY_ALIASES were unreachable. The real-time API states no grade, so None is
    correct for it; a grade supplied by the archive must be read.
    """
    import json

    measures = json.loads((FIXTURES / "ea_measures_E21136.json").read_text())

    def with_measures(payload):
        def _fetch(url):
            if "measures" in url:
                return json.dumps(payload)
            return (FIXTURES / "ea_station_E21136.json").read_text()
        return _fetch

    # As the live service actually publishes it: no grade anywhere.
    assert ea.readings(STATION, fetch=with_measures(measures))[0].value.quality is None

    on_reading = json.loads(json.dumps(measures))
    on_reading["items"][0]["latestReading"]["quality"] = "Unchecked"
    assert ea.readings(
        STATION, fetch=with_measures(on_reading))[0].value.quality == "provisional"

    on_measure = json.loads(json.dumps(measures))
    on_measure["items"][0]["quality"] = "Suspect"
    assert ea.readings(
        STATION, fetch=with_measures(on_measure))[0].value.quality == "unverified"


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
