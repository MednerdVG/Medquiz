"""Validation engine, suggestions and follow-up cloning (brief §12, §15, §16; acceptance 10–11)."""
import pytest

from app.rx.schema import MedRow
from app.rx.followup import change_summary, clone_for_follow_up
from app.rx.suggest import suggest
from app.rx.validate.rules import BLOCK, WARN, blocking, unacknowledged, validate


def codes(issues, level=None):
    return {i.code for i in issues if level is None or i.level == level}


def warn_msgs(issues):
    return " | ".join(i.message for i in issues if i.level == WARN)


def with_started(rx_factory, *rows, **kw):
    return rx_factory(medications__started=list(rows), **kw)


def test_clean_example_has_no_blocking(rx_factory):
    assert blocking(validate(rx_factory())) == []


# ---------------------------------------------------------------- acceptance 10
@pytest.mark.parametrize("name", [
    "Tab. Vonoprazan 50 mg", "Tab. Restyl 0.025", "Tab. Levothyroxine 7.5 mcg",
    "Tab. Ciplar LA 10 mg", "Tab. Montair LC 10/10", "Tab. Alsita-M 10/500",
])
def test_strength_not_in_formulary_warns(rx_factory, name):
    rx = with_started(rx_factory, {"name": name, "dose": "1–0–0"})
    issues = validate(rx)
    assert "strength:started:0" in codes(issues, WARN), warn_msgs(issues)


@pytest.mark.parametrize("name", ["Tab. Xilingio 25/5", "Tab. Montair LC 10/5", "Tab. Thyronorm 50 mcg", "Tab. Ciplar LA 40"])
def test_marketed_strength_passes(rx_factory, name):
    issues = validate(with_started(rx_factory, {"name": name, "dose": "1–0–0"}))
    assert "strength:started:0" not in codes(issues)


def test_medicine_without_dose_blocks_approval(rx_factory):
    rx = with_started(rx_factory, {"name": "Tab. Jardiance 10"})
    assert "med_dose:started:0" in codes(validate(rx), BLOCK)


def test_missing_identity_blocks(rx_factory):
    rx = rx_factory(meta__seen_by=None, patient__name="")
    assert {"missing:seen_by", "missing:patient_name"} <= codes(validate(rx), BLOCK)


def test_same_molecule_started_and_continued_blocks(rx_factory):
    rx = rx_factory(medications__started=[{"name": "Tab. Rozavel 20", "dose": "0–0–1"}])  # rosuvastatin; Rosuvas continued
    assert any(c.startswith("dup_started_continued") for c in codes(validate(rx), BLOCK))


def test_stopped_and_active_blocks(rx_factory):
    rx = rx_factory(medications__stopped=[{"name": "Tab. Rosuvas 10", "reason": "muscle pain"}])
    assert any(c.startswith("dup_stopped_active") for c in codes(validate(rx), BLOCK))


def test_stored_record_mismatch_blocks(rx_factory):
    rx = rx_factory()
    issues = validate(rx, stored_patient={"name": "Mrs. Afsha Khan", "age_years": 55, "sex": "M", "patient_id": "MFX-00231"})
    assert "record_mismatch:sex" in codes(issues, BLOCK)
    ok = validate(rx, stored_patient={"name": "Afsha Khan", "age_years": 56, "sex": "F"})
    assert not any(c.startswith("record_mismatch") for c in codes(ok))


def test_upload_for_other_patient_blocks(rx_factory):
    issues = validate(rx_factory(), uploads=[{"id": "u1", "filename": "hba1c.pdf", "extracted_patient_name": "Rohan Mehta"}])
    assert "upload_mismatch:u1" in codes(issues, BLOCK)


@pytest.mark.parametrize("row,expect", [
    ({"name": "Inj. Sybrava 284 mg", "dose": "once a week"}, "schedule:started:0"),
    ({"name": "Inj. Mounjaro 5 mg", "dose": "1–0–0", "timing": "daily"}, "schedule:started:0"),
    ({"name": "Inj. Awiqli", "dose": "70 units", "timing": "daily at night"}, "schedule:started:0"),
])
def test_schedule_mismatch_warns(rx_factory, row, expect):
    assert expect in codes(validate(with_started(rx_factory, row)), WARN)


def test_weekly_glp1_written_weekly_is_fine(rx_factory):
    rx = with_started(rx_factory, {"name": "Inj. Mounjaro 5 mg", "dose": "once a week", "timing": "every Monday"})
    assert "schedule:started:0" not in codes(validate(rx))


@pytest.mark.parametrize("vitals,code", [
    ({"strip": [{"label": "WAIST", "value": "38 cm"}]}, "implausible:waist"),
    ({"weight_kg": 300, "height_cm": 150, "strip": []}, "implausible:bmi"),
    ({"strip": [{"label": "BP", "value": "300/180 mmHg"}]}, "implausible:bp"),
    ({"pulse": 20, "strip": []}, "implausible:pulse"),
    ({"height_cm": 65, "weight_kg": 70, "strip": []}, "implausible:height"),
])
def test_implausible_measurements(rx_factory, vitals, code):
    assert code in codes(validate(rx_factory(vitals=vitals)), WARN)


def test_interaction_warnings(rx_factory):
    rx = rx_factory(advice_blocks=[], medications__started=[
        {"name": "Inj. Mounjaro 2.5 mg", "dose": "once a week"},
        {"name": "Tab. Amaryl 1", "dose": "1–0–0"},
        {"name": "Tab. Jardiance 10", "dose": "1–0–0"},
    ])
    c = codes(validate(rx), WARN)
    assert {"interaction:su_insulin_glp1", "interaction:sglt2_sickday"} <= c


def test_two_brands_same_molecule_warns(rx_factory):
    rx = rx_factory(medications__nutrition=[], medications__started=[{"name": "Tab. Glycomet 500", "dose": "1–0–1"}],
                    medications__continued=[{"name": "Tab. Istamet 50/500", "dose": "1–0–1"}])
    assert "dup_molecule:metformin" in codes(validate(rx), WARN)


def test_statin_intolerance_and_nsaid_kidney(rx_factory):
    rx = rx_factory(patient__intolerances=["statin — myalgia"], patient__known_conditions=["CKD stage 3"],
                    medications__continued=[], medications__started=[{"name": "Tab. Atorva 10", "dose": "0–0–1"}],
                    medications__sos=[{"name": "Tab. Brufen 400", "note": "for pain"}])
    c = codes(validate(rx), WARN)
    assert {"interaction:statin_intolerance", "interaction:nsaid_kidney"} <= c


def test_age_related(rx_factory):
    rx = rx_factory(medications__started=[{"name": "Tab. Zolfresh 10", "dose": "0–0–1"}, {"name": "Tab. Restyl 0.25", "dose": "0–0–1"}],
                    patient__age_years=70)
    c = codes(validate(rx), WARN)
    assert any(x.startswith("age:zolpidem") for x in c) and any(x.startswith("age:benzo") for x in c)


def test_followup_investigation_pairing(rx_factory):
    assert "followup_no_investigations" in codes(validate(rx_factory(investigations=[])), WARN)
    assert "investigations_no_followup" in codes(validate(rx_factory(follow_up=None)), WARN)


def test_referral_mentioned_but_missing(rx_factory):
    rx = rx_factory(plain_terms={"title": "X", "paragraphs": ["We will refer you to Psychiatry after the reports."]})
    assert "referral_missing:psychiatry" in codes(validate(rx), WARN)
    rx = rx_factory(plain_terms={"title": "X", "paragraphs": ["We will refer you to Psychiatry after the reports."]},
                    referrals=[{"to": "Psychiatry", "named_doctor": "Dr. Mukul", "contact": "8329726113"}])
    assert "referral_missing:psychiatry" not in codes(validate(rx))


def test_acknowledgement():
    from app.rx.validate.rules import Issue
    issues = [Issue(WARN, "a", "x"), Issue(WARN, "b", "y"), Issue(BLOCK, "c", "z")]
    assert [i.code for i in unacknowledged(issues, ["a"])] == ["b"]


# ---------------------------------------------------------------- acceptance 11 (suggestion half)
def test_tirzepatide_suggests_glp1_block(rx_factory):
    rx = rx_factory(advice_blocks=[], patient__age_years=34, patient__extra_rows=[],
                    medications__started=[{"name": "Inj. Mounjaro 2.5 mg", "generic": "tirzepatide", "dose": "once a week"}])
    s = {x.template for x in suggest(rx)}
    assert "glp1_weekly" in s and "contraception_on_glp1" in s
    assert rx.advice_blocks == []  # suggest only, never auto-insert


def test_suggestions_by_class(rx_factory):
    rx = rx_factory(advice_blocks=[], medications__started=[{"name": "Tab. Thyronorm 50 mcg", "dose": "1–0–0"},
                                                            {"name": "Tab. Atorva 10", "dose": "0–0–1"}])
    s = {x.template for x in suggest(rx)}
    assert {"thyroxine_timing", "statin_muscle_pain"} <= s and "hypoglycaemia" not in s


# ---------------------------------------------------------------- follow-up
def test_clone_and_change_summary(rx_factory):
    prev = rx_factory()
    nxt = clone_for_follow_up(prev)
    assert nxt.meta.consult_type == "follow_up" and nxt.meta.visit_number == 2
    assert nxt.meta.previous_consultation_id == prev.meta.consultation_id
    assert nxt.medications.started == [] and len(nxt.medications.continued) == 4
    assert nxt.labs == [] and all(c.value == "" for c in nxt.vitals.strip)
    nxt.medications.continued = [r for r in nxt.medications.continued if "Glycomet" not in r.name]
    nxt.medications.continued[0].dose = "1–0–1"
    nxt.medications.started = [MedRow(name="Inj. Mounjaro 2.5 mg", dose="once a week")]
    cs = change_summary(prev, nxt)
    assert cs.started == ["Inj. Mounjaro 2.5 mg"]
    assert cs.stopped == ["Tab. Glycomet SR 500"]
    assert len(cs.dose_changed) == 1 and "→" in cs.dose_changed[0]
    assert "Started: Inj. Mounjaro 2.5 mg" in cs.lines()
