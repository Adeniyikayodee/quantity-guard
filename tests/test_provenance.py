import pytest

from quantity_guard import Q, datums, quantity_tool, session
from quantity_guard.packs.water import register_station, station_spec


@quantity_tool(params={"station": {}}, returns={"unit": "cfs"})
def observed_discharge(station):
    """Stand-in for a gage retrieval."""
    return Q(1250.0, "cfs", quality="provisional", source=f"usgs:{station}")


def test_tool_outputs_are_recorded():
    with session() as s:
        observed_discharge("07374000")
    assert len(s.outputs) == 1
    assert s.outputs[0].quantity.magnitude == pytest.approx(1250)


def test_a_stated_value_matching_a_tool_output_is_sourced():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Discharge is 1250 cfs at the gage.")
    assert audit.ok
    assert audit.claims[0].status == "sourced"


def test_a_value_restated_in_another_unit_is_still_sourced():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Discharge is 35.4 m3/s.")
    assert audit.ok


def test_a_rounded_restatement_is_sourced():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Discharge is about 1250.4 cfs.")
    assert audit.ok


def test_a_fabricated_value_is_unsourced():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Peak discharge will reach 4200 cfs tomorrow.")
    assert not audit.ok
    assert [c.text for c in audit.unsourced] == ["4200 cfs"]


def test_a_correct_magnitude_in_the_wrong_unit_is_flagged():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Discharge is 1250 m3/s.")
    assert not audit.ok
    assert audit.mislabelled[0].detail.endswith("not m3/s")


def test_years_and_small_counts_are_ignored():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Across 3 sites since 1993, discharge is 1250 cfs.")
    assert audit.ok


def test_datum_names_are_not_read_as_measurements():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Discharge is 1250 cfs, stage is on NAVD88 not NGVD29.")
    found = [c.text for c in audit.claims]
    assert "88" not in found and "29" not in found
    assert audit.ok


def test_station_identifiers_are_ignored():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("Station 07374000 reports 1250 cfs.")
    assert audit.ok


def test_a_stated_duration_is_audited():
    with session() as s:
        observed_discharge("07374000")
        audit = s.audit_answer("The peak arrives within 36 hours.")
    assert [c.text for c in audit.unsourced] == ["36 hours"]
