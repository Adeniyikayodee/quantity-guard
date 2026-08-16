"""Retrieval from USGS Water Services.

Run against responses recorded from the live service, so the tests neither need the
network nor break when the river changes.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quantity_guard import DatumMismatch, Q, QualityViolation, datums, quantity_tool
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
    (["E"], "estimated"), (["p"], "provisional"),
    # Condition codes. These were previously dropped, so an ice-affected or
    # equipment-affected reading came back indistinguishable from clean record.
    (["Ice"], "unverified"), (["Bkw"], "unverified"), (["Eqp"], "unverified"),
    (["Fld"], "unverified"), (["Dis"], "unverified"), (["***"], "unverified"),
    (["Rat"], "estimated"), (["ZFl"], "estimated"),
    # The weakest flag present wins, whichever family it comes from.
    (["A", "Ice"], "unverified"), (["P", "Ice"], "unverified"), (["P", "e"], "provisional"),
    # A footnote code that says nothing about quality is ignored, not an error.
    (["Zz"], None), (["A", "Zz"], "approved"),
])
def test_qualifiers_become_quality_flags(qualifiers, expected):
    assert Q(1.0, "ft", quality=usgs._quality(qualifiers)).quality == expected


@pytest.mark.parametrize("code,expected", [
    # NWIS publishes "deg C"; the table is keyed on "deg c". The mismatch made water
    # temperature unretrievable.
    ("deg C", "degC"), ("deg c", "degC"), ("DEG C", "degC"),
    ("ft3/s", "foot**3/second"), ("FNU", "FNU"), ("NTU", "NTU"),
    ("std units", "pH_unit"), ("%", "percent"), ("ac-ft", "acre*foot"),
    ("ft/sec", "foot/second"), ("uS/cm @25C", "microsiemens/centimetre"),
])
def test_unit_codes_are_matched_case_insensitively(code, expected):
    assert usgs.unit_for(code) == expected


def test_an_unmapped_parameter_does_not_cost_the_whole_station():
    """A water-quality sensor with an unknown unit must not take discharge with it."""
    payload = json.loads((FIXTURES / "usgs_iv_07374000.json").read_text())
    series = payload["value"]["timeSeries"]
    exotic = json.loads(json.dumps(series[0]))
    exotic["variable"]["variableCode"][0]["value"] = "99999"
    exotic["variable"]["unit"]["unitCode"] = "smoots per fortnight"
    series.append(exotic)

    with pytest.warns(UserWarning, match="skipping USGS parameter 99999"):
        values = usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))

    assert "00060" in values and "00065" in values
    assert "99999" not in values


@pytest.mark.parametrize("left,right", [("NTU", "FNU"), ("NTU", "pH_unit"),
                                        ("pH_unit", "percent")])
def test_index_units_do_not_interconvert(left, right):
    """NTU and FNU are different instrument standards, and neither is a pH."""
    with pytest.raises(Exception):
        Q(1.0, left).to(right)


def _site_rdb(**overrides):
    """The recorded site record with named columns overridden."""
    lines = (FIXTURES / "usgs_site_07374000.rdb").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("agency_cd"))
    header = lines[start].split("\t")
    row = lines[start + 2].split("\t")
    for column, value in overrides.items():
        row[header.index(column)] = value
    return "\n".join(lines[:start + 2] + ["\t".join(row)])


def test_an_unstated_altitude_datum_is_not_assumed():
    """The pack defaulted alt_datum_cd to NAVD88.

    Refusing to guess an offset between datums while guessing which datum a published
    altitude is measured from is the same error one step earlier, and every stage
    converted through the resulting offset inherits it silently.
    """
    with pytest.warns(UserWarning, match="no alt_datum_cd"):
        record = usgs.site("TEST_NODATUM", fetch=lambda url: _site_rdb(alt_datum_cd=""))
    assert record.gage_datum is None
    assert not datums.can_convert(record.datum_name, "NAVD88")


def test_a_stated_altitude_datum_is_read_not_defaulted():
    record = usgs.site("TEST_NGVD", fetch=lambda url: _site_rdb(alt_datum_cd="NGVD29"))
    assert record.gage_datum.datum == "NGVD29"
    assert datums.can_convert(record.datum_name, "NGVD29")


def test_a_site_without_an_altitude_still_registers_its_own_datum():
    """Stage must stay labelled with the station frame even with no published altitude.

    Registration was previously nested inside the altitude branch, so such a site left
    `GAGE:...` unregistered and every stage reading raised.
    """
    record = usgs.site("TEST_NOALT",
                       fetch=lambda url: _site_rdb(alt_va="", alt_datum_cd=""))
    assert record.gage_datum is None
    assert record.datum_name in datums.datums
    assert Q(12.4, "ft", datum=record.datum_name).datum == record.datum_name


def test_the_published_altitude_accuracy_is_carried():
    record = usgs.site("TEST_ACY", fetch=lambda url: _site_rdb())
    assert record.gage_datum_accuracy.to("ft").magnitude == pytest.approx(0.01)


def _payload_dated(discharge_at: str, stage_at: str | None = None):
    """The recorded IV payload with its reading timestamps restamped.

    The recording is itself a day old, so a test about staleness has to set both stamps
    rather than leaning on whatever the fixture happens to carry.
    """
    payload = json.loads((FIXTURES / "usgs_iv_07374000.json").read_text())
    series = payload["value"]["timeSeries"]
    series[0]["values"][0]["value"][0]["dateTime"] = discharge_at
    if stage_at is not None:
        series[1]["values"][0]["value"][0]["dateTime"] = stage_at
    return payload


@pytest.mark.parametrize("published", [
    # The literal string comparison caught only the first of these, so a missing value
    # came back as a real discharge of -999,999 ft3/s, carrying a genuine quality flag
    # and passing every guard.
    "-999999", "-999999.0", "-999999.00", "-9.99999E5", "", "   ", "Ice",
])
def test_every_spelling_of_the_missing_value_sentinel_is_dropped(published):
    payload = json.loads((FIXTURES / "usgs_iv_07374000.json").read_text())
    payload["value"]["timeSeries"][0]["values"][0]["value"][0]["value"] = published
    values = usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))
    assert "00060" not in values


def test_a_real_measurement_is_not_mistaken_for_the_sentinel():
    values = usgs.instantaneous(SITE, fetch=fetch)
    assert values["00060"].value.magnitude == pytest.approx(234000)


def test_the_sentinel_is_read_from_the_variable_not_hard_coded():
    """NWIS publishes noDataValue per variable, in the same payload."""
    payload = json.loads((FIXTURES / "usgs_iv_07374000.json").read_text())
    series = payload["value"]["timeSeries"][0]
    series["variable"]["noDataValue"] = -8888.0
    series["values"][0]["value"][0]["value"] = "-8888.0"
    assert "00060" not in usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))


def test_a_reading_knows_how_old_it_is():
    fresh = datetime.now(timezone.utc).isoformat()
    values = usgs.instantaneous(
        SITE, fetch=lambda url: json.dumps(_payload_dated(fresh)))
    assert values["00060"].age < timedelta(minutes=5)
    assert not values["00060"].is_stale(timedelta(hours=1))


def test_a_stale_reading_is_dropped_when_a_max_age_is_given():
    """The service serves the last value it holds regardless of age.

    At a real site one call can mix a reading minutes old with one years old, and nothing
    downstream can recover the distinction, because the ledger records the magnitude and
    unit but not when it was measured.
    """
    # A seven-year-old discharge beside a current stage, which is the shape the live
    # service really returns at a site with a retired sensor.
    payload = _payload_dated("2019-03-02T06:00:00.000-06:00",
                             datetime.now(timezone.utc).isoformat())

    stale = usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))
    assert stale["00060"].age > timedelta(days=1000)

    with pytest.warns(UserWarning, match="dropping USGS parameter 00060"):
        filtered = usgs.instantaneous(
            SITE, fetch=lambda url: json.dumps(payload), max_age=timedelta(hours=6))
    assert "00060" not in filtered
    # The current stage in the same response survives the stale discharge being dropped.
    assert "00065" in filtered


def test_max_age_defaults_to_returning_whatever_the_service_holds():
    payload = _payload_dated("2019-03-02T06:00:00.000-06:00")
    assert "00060" in usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))


def test_an_ice_affected_reading_cannot_clear_an_approved_gate():
    """The condition code has to survive retrieval and reach the tool boundary."""
    payload = json.loads((FIXTURES / "usgs_iv_07374000.json").read_text())
    payload["value"]["timeSeries"][0]["values"][0]["value"][0]["qualifiers"] = ["A", "Ice"]
    values = usgs.instantaneous(SITE, fetch=lambda url: json.dumps(payload))
    assert values["00060"].value.quality == "unverified"

    @quantity_tool(params={"q": {"unit": "m**3/s", "quality": "approved"}})
    def publish(q):
        return q

    with pytest.raises(QualityViolation):
        publish(values["00060"].value)


# Live service -----------------------------------------------------------------------------


@pytest.mark.live
def test_against_the_live_service():
    """Run with -m live to check the recorded fixtures still match the real shape."""
    record, values = usgs.reading(SITE)
    assert record.name
    assert values["00060"].value.dimensionality == Q(1, "cfs").dimensionality
