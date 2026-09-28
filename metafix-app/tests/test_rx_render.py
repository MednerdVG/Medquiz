"""Prescription engine — acceptance tests 5–11 and 13 (brief §21) plus unit tests."""
import re

import pytest

from app.rx.bmi import bmi_class, compute_bmi
from app.rx.render.packer import ChunkTooTall, PackItem, pack
from app.rx.render.renderer import RenderError, file_name, render
from app.rx.schema import Prescription
from tests.conftest import pdf_text, squash


@pytest.fixture(scope="module")
def acceptance_render():
    import json
    from tests.conftest import EXAMPLES
    rx = Prescription.model_validate(json.loads((EXAMPLES / "acceptance_first_visit.json").read_text()))
    return rx, render(rx)


# ---------------------------------------------------------------- unit
def test_bmi_asian_indian():
    assert compute_bmi(99.8, 165) == 36.7
    assert bmi_class(36.7) == "Obesity III"
    assert bmi_class(23.0) == "Overweight"
    assert bmi_class(25.0) == "Obesity I"
    assert bmi_class(30.0) == "Obesity II"
    assert compute_bmi(None, 165) is None and compute_bmi(80, None) is None


def test_packer_minimises_pages_then_balances():
    items = [PackItem(str(i), h) for i, h in enumerate([60, 60, 60, 60, 60])]
    pages = pack(items, 200, 200)
    assert len(pages) == 2
    assert sorted(len(p) for p in pages) == [2, 3]
    assert [i for p in pages for i in p] == list(range(5))  # order preserved


def test_packer_respects_first_page_capacity():
    items = [PackItem("a", 150), PackItem("b", 70)]
    assert pack(items, 210, 219) == [[0], [1]]


def test_packer_rejects_oversized_chunk():
    with pytest.raises(ChunkTooTall) as exc:
        pack([PackItem("Diet plan", 300)], 210, 219)
    assert "Diet plan" in str(exc.value)


def test_packer_joined_row_groups_can_share_a_page():
    items = [PackItem("t1", 110, group="g", joined_height=100), PackItem("t2", 110, group="g", joined_height=100)]
    assert pack(items, 210, 219) == [[0, 1]]


def test_file_name(rx_factory):
    rx = rx_factory()
    assert file_name(rx) == "Metafix_Prescription_Mrs_Afsha_Khan_28Sep2026.pdf"
    rx.meta.version = 2
    assert file_name(rx).endswith("_28Sep2026_v2.pdf")


# ---------------------------------------------------------------- acceptance 5
def test_first_visit_renders_4_to_5_pages_with_name_on_every_page(acceptance_render):
    rx, res = acceptance_render
    pages = pdf_text(res.pdf)
    assert 4 <= len(pages) <= 5, len(pages)
    assert res.page_count == len(pages)
    for n, text in enumerate(pages, 1):
        assert f"Page {n} of {len(pages)}" in text
        assert "ROHANMEHTA" in squash(text.upper())


# ---------------------------------------------------------------- acceptance 6
def test_render_is_byte_identical(acceptance_render):
    rx, res = acceptance_render
    again = render(rx)
    assert again.pdf == res.pdf


# ---------------------------------------------------------------- acceptance 7
def test_second_patient_contains_no_data_from_the_first(acceptance_render, rx_factory):
    _, first = acceptance_render
    second = render(rx_factory())
    text = " ".join(pdf_text(second.pdf)).upper()
    for leaked in ("ROHAN", "MEHTA", "MFX-00412", "MOUNJARO"):
        assert leaked not in text


# ---------------------------------------------------------------- acceptance 8
def test_signature_blocks_per_doctor(acceptance_render, rx_factory):
    _, shubham = acceptance_render
    assert 'data-doctor="shubham_ahirrao"' in shubham.html
    assert "<img src=\"data:image/png" in shubham.html.split('data-doctor="shubham_ahirrao"')[1][:200]
    s_text = " ".join(pdf_text(shubham.pdf))
    assert "DM (Endocrinology)" in s_text and "Vishal" not in s_text

    vishal = render(rx_factory())
    v_block = vishal.html.split('data-doctor="vishal_gabale"')[1][:600]
    assert "<img" not in v_block  # typed style
    v_text = " ".join(pdf_text(vishal.pdf))
    assert "Dr. Vishal Gabale" in v_text and "Shubham" not in v_text


# ---------------------------------------------------------------- acceptance 9
def test_certificate_has_blank_signature_and_stamp_box(rx_factory):
    rx = rx_factory(meta__seen_by="shubham_ahirrao",
                    certificates=[{"type": "treatment_certificate", "fields": {"treatment_period": "since 28 September 2026"}}])
    res = render(rx)
    cert_html = res.html.split('data-kind="cert"')[1]
    assert "Clinic stamp" in cert_html and 'class="ruled"' in cert_html
    assert "<img" not in cert_html.split('class="lh-brand"')[1].split("</section>")[0].split('class="page-body"')[1]
    last = pdf_text(res.pdf)[-1]
    assert "MEDICALCERTIFICATE" in squash(last) and "SIGNATURE&SEAL" in squash(last).upper()
    assert "Dr. Shubham Ahirrao" in last


def test_certificate_prints_legal_name(rx_factory):
    rx = rx_factory(certificates=[{"type": "fitness_certificate", "fields": {"fit_for": "resume work"}}])
    last = pdf_text(render(rx).pdf)[-1]
    assert "Dr. Vishal Raghunath Gabale" in last


def test_insurance_summary_is_two_pages(rx_factory):
    base = render(rx_factory())
    res = render(rx_factory(certificates=["insurance_summary_letter"]))
    assert res.page_count == base.page_count + 2


# ---------------------------------------------------------------- acceptance 11 (render half)
def test_glp1_block_carries_supply_contact_when_tirzepatide_prescribed(acceptance_render, rx_factory):
    _, res = acceptance_render
    text = " ".join(pdf_text(res.pdf))
    assert text.count("9920498280") == 1
    no_glp1 = render(rx_factory(advice_blocks=["glp1_weekly"]))
    assert "9920498280" not in " ".join(pdf_text(no_glp1.pdf))


# ---------------------------------------------------------------- acceptance 13
def test_overstuffed_section_fails_with_readable_error(rx_factory):
    rx = rx_factory(plain_terms={"title": "WHAT THIS MEANS", "paragraphs": ["A long explanation. " * 60] * 12})
    with pytest.raises(RenderError) as exc:
        render(rx)
    msg = str(exc.value)
    assert "Plain-terms box" in msg and "cannot be printed without clipping" in msg


# ---------------------------------------------------------------- rules
def test_empty_sections_are_omitted(rx_factory):
    rx = rx_factory(labs=[], investigations=[], referrals=[], diet_plan=None, exercise_plan=None,
                    advice_blocks=[], charts=[], plain_terms=None, labs_footnote=None)
    text = squash(" ".join(pdf_text(render(rx).pdf)).upper())
    for heading in ("LABORATORYFINDINGS", "INVESTIGATIONSADVISED", "REFERRAL", "DIETPLAN", "EXERCISEPLAN", "N/A"):
        assert heading not in text


def test_no_render_without_seen_by(rx_factory):
    with pytest.raises(RenderError):
        render(rx_factory(meta__seen_by=None))


def test_bmi_printed_from_weight_and_height(rx_factory):
    text = " ".join(pdf_text(render(rx_factory()).pdf))
    assert "36.7 — Obesity III" in text and "Asian-Indian" in text
    text = " ".join(pdf_text(render(rx_factory(vitals__height_cm=None)).pdf))
    assert "Obesity III" not in text


def test_medicine_anchors_in_diet(rx_factory):
    html = render(rx_factory()).html
    assert re.search(r'class="anchor">Tab\. Rosuvas 10 — after dinner', html)
    off = render(rx_factory(meta__medicine_anchors=False)).html
    assert 'class="anchor"' not in off


def test_follow_up_title_and_continuation_strip(rx_factory):
    rx = rx_factory(meta__consult_type="follow_up", meta__visit_number=2,
                    interval_review=["Sugars still above target on the starting dose"])
    res = render(rx)
    pages = pdf_text(res.pdf)
    assert "FOLLOW-UP1" in squash(pages[0])
    assert "INTERVALREVIEW" in squash(pages[0])
    assert all("MRS.AFSHAKHAN·28SEPTEMBER2026·CONTINUED" in squash(p) for p in pages[1:])


def test_dual_signatories(rx_factory):
    res = render(rx_factory(meta__co_signatory="adwait_mastud"))
    assert 'data-doctor="vishal_gabale"' in res.html and 'data-doctor="adwait_mastud"' in res.html
