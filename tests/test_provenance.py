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


def test_carry_over_between_tools_is_refused():
    from quantity_guard import UnconvertedCarryOver

    @quantity_tool(params={"discharge": {"unit": "m**3/s"}}, returns={"unit": "m**3/s"})
    def downstream(discharge):
        return discharge

    with session():
        observed_discharge("07374000")  # returns 1250 cfs
        with pytest.raises(UnconvertedCarryOver) as exc:
            downstream(1250)
        assert '"unit": "cfs"' in exc.value.message
        # The same value sent with its unit converts correctly.
        assert downstream({"value": 1250, "unit": "cfs"}).magnitude == pytest.approx(35.4, rel=1e-2)


def test_carry_over_check_does_not_fire_outside_a_session():
    @quantity_tool(params={"discharge": {"unit": "m**3/s"}})
    def downstream(discharge):
        return discharge

    assert downstream(1250).magnitude == 1250


def test_derived_values_can_be_registered():
    with session() as s:
        observed_discharge("07374000")
        s.record_derived(Q(17.1, "ft"), note="freeboard")
        audit = s.audit_answer("Freeboard is 17.1 ft.")
    assert audit.ok


def test_manifest_captures_units_and_quality():
    with session() as s:
        observed_discharge("07374000")
        manifest = s.manifest()
    entry = manifest["quantities"][-1]
    assert entry["unit"] == "cfs"
    assert entry["quality"] == "provisional"
    assert manifest["calls"] == 1


def test_station_registration_enables_a_datum_shift():
    name = register_station("TESTSTN", Q(1.5, "ft", datum="NAVD88"))
    assert name == "GAGE:TESTSTN"
    stage = Q(12.4, "ft", datum=name)
    assert stage.to_datum("NAVD88").magnitude == pytest.approx(13.9)


def test_station_spec_binds_the_local_datum():
    register_station("TESTSTN2", Q(2.0, "ft", datum="NAVD88"))
    spec = station_spec("TESTSTN2")
    assert spec.datum == "GAGE:TESTSTN2"
    assert spec.coerce(12.4, "stage").datum == "GAGE:TESTSTN2"


def test_a_dropped_datum_is_caught_like_a_dropped_unit():
    """The magnitude is right and the unit is right; only the reference is wrong."""
    from quantity_guard import UnconvertedCarryOver

    datums.register("GAGE:DROP")
    datums.register_offset("GAGE:DROP", "NAVD88", Q(1.5, "ft"))

    @quantity_tool(returns={"unit": "ft", "datum": "GAGE:DROP"})
    def read_stage():
        return Q(12.4, "ft", datum="GAGE:DROP")

    @quantity_tool(params={"stage": {"unit": "ft", "datum": "NAVD88"}}, returns={"unit": "ft"})
    def elevation(stage):
        return stage

    with session():
        read_stage()
        with pytest.raises(UnconvertedCarryOver) as exc:
            elevation(12.4)
        assert "different references" in exc.value.message
        # Shifting it first is accepted.
        assert elevation(Q(12.4, "ft", datum="GAGE:DROP").to_datum("NAVD88")).magnitude == \
            pytest.approx(13.9)


def test_a_matching_magnitude_on_the_same_datum_is_not_flagged():
    @quantity_tool(returns={"unit": "ft", "datum": "NAVD88"})
    def read_flood():
        return Q(31.0, "ft", datum="NAVD88")

    @quantity_tool(params={"stage": {"unit": "ft", "datum": "NAVD88"}}, returns={"unit": "ft"})
    def elevation(stage):
        return stage

    with session():
        read_flood()
        assert elevation(31.0).magnitude == pytest.approx(31.0)
