"""Prescription data model (brief §10) — the single source of truth.

Every consultation form field maps one-to-one onto a field here, and the
renderer reads nothing else. Empty lists / None mean "omit the section".
"""
from __future__ import annotations

from datetime import date
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- meta
class Meta(_M):
    consultation_id: str
    booking_id: Optional[str] = None
    consult_date: date
    consult_type: Literal["in_clinic", "teleconsult", "follow_up"] = "in_clinic"
    visit_number: int = 1
    previous_consultation_id: Optional[str] = None
    seen_by: Optional[str] = None  # required at validation — no render without it
    co_signatory: Optional[str] = None  # second doctor for dual-signed prescriptions
    created_by: Optional[str] = None
    section_order_preset: Literal["standard", "medication_last", "two_page_clinical"] = "standard"
    status: Literal["draft", "approved", "rendered"] = "draft"
    version: int = 1
    amendment_note: Optional[str] = None
    # Per-document section title overrides, keyed by section key (e.g. "diet").
    section_titles: dict[str, str] = Field(default_factory=dict)
    medicine_anchors: bool = True  # §12 medicine anchors in the diet plan


# ---------------------------------------------------------------- patient
class LabelValue(_M):
    label: str
    value: str


class Patient(_M):
    name: str = ""
    age_years: Optional[int] = None
    sex: Optional[Literal["M", "F", "O"]] = None
    patient_id: Optional[str] = None
    diet_type: Optional[str] = None
    allergies: list[str] = Field(default_factory=list)
    known_conditions: list[str] = Field(default_factory=list)
    family_history: list[str] = Field(default_factory=list)
    intolerances: list[str] = Field(default_factory=list)  # e.g. "statin"
    extra_rows: list[LabelValue] = Field(default_factory=list)


class VitalCell(_M):
    label: str
    value: str
    sub: Optional[str] = None
    earlier: Optional[str] = None  # "earlier value" annotation


class Vitals(_M):
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    waist_cm: Optional[float] = None
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    auto_bmi: bool = True  # insert/replace the BMI cell from weight + height
    strip: list[VitalCell] = Field(default_factory=list)


# ---------------------------------------------------------------- clinical
class Complaint(_M):
    text: str
    pointer: Optional[str] = None


class Diagnosis(_M):
    text: str
    detail: Optional[str] = None


class Lab(_M):
    test: str
    value: str
    remark: Optional[str] = None
    highlight: bool = False
    earlier: Optional[str] = None
    source: Optional[str] = None
    date: Optional[str] = None


class PlainTerms(_M):
    title: str = "WHAT THIS MEANS IN PLAIN TERMS"
    paragraphs: list[str] = Field(default_factory=list)


class OptionRow(_M):
    label: str
    values: list[str]


class TreatmentOptions(_M):
    intro: Optional[str] = None
    columns: list[str] = Field(default_factory=list)
    rows: list[OptionRow] = Field(default_factory=list)
    recommendation: Optional[str] = None


class Investigation(_M):
    name: str
    instruction: Optional[str] = None
    purpose: Optional[str] = None
    timing: Optional[str] = None  # "now" | "after 1 month" | "at 3 months" ...


# ---------------------------------------------------------------- medication
class MedRow(_M):
    name: str
    generic: Optional[str] = None
    dose: Optional[str] = None  # dose notation: 1–0–1, SOS, weekly (Monday) ...
    timing: Optional[str] = None
    duration: Optional[str] = None
    note: Optional[str] = None
    purpose: Optional[str] = None
    reason: Optional[str] = None  # stopped list
    route: Optional[str] = None  # administered list
    start_after: Optional[str] = None  # sequenced courses


class Medications(_M):
    stopped: list[MedRow] = Field(default_factory=list)
    started: list[MedRow] = Field(default_factory=list)
    continued: list[MedRow] = Field(default_factory=list)
    sos: list[MedRow] = Field(default_factory=list)
    administered: list[MedRow] = Field(default_factory=list)
    nutrition: list[MedRow] = Field(default_factory=list)
    footnotes: list[str] = Field(default_factory=list)

    ACTIVE: ClassVar[tuple[str, ...]] = ("started", "continued", "sos", "administered", "nutrition")

    def all_rows(self) -> list[tuple[str, MedRow]]:
        out: list[tuple[str, MedRow]] = []
        for k in ("stopped", *self.ACTIVE):
            out.extend((k, r) for r in getattr(self, k))
        return out

    def is_empty(self) -> bool:
        return not any(getattr(self, k) for k in ("stopped", *self.ACTIVE))


class TaperPlan(_M):
    medicine: str
    instruction: str


class PlanRef(_M):
    template: str
    title_override: Optional[str] = None
    overrides: dict = Field(default_factory=dict)


class Referral(_M):
    to: str
    reason: Optional[str] = None
    named_doctor: Optional[str] = None
    contact: Optional[str] = None


class Contact(_M):
    label: str
    name: Optional[str] = None
    number: Optional[str] = None


class FollowUp(_M):
    interval: Optional[str] = None
    bring: list[str] = Field(default_factory=list)
    report_sooner_if: list[str] = Field(default_factory=list)


class FreeText(_M):
    section_title: str
    body_html: str


class CertificateRequest(_M):
    """Either a bare type string or a dict with per-certificate fields."""
    type: Literal[
        "treatment_certificate",
        "fitness_certificate",
        "medical_leave_certificate",
        "insurance_summary_letter",
    ]
    fields: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- root
class Prescription(_M):
    meta: Meta
    patient: Patient
    vitals: Vitals = Field(default_factory=Vitals)
    complaints: list[Complaint] = Field(default_factory=list)
    interval_review: list[str] = Field(default_factory=list)
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    labs: list[Lab] = Field(default_factory=list)
    labs_footnote: Optional[str] = None  # "relevant normals" footnote
    plain_terms: Optional[PlainTerms] = None
    treatment_options: Optional[TreatmentOptions] = None
    investigations: list[Investigation] = Field(default_factory=list)
    medications: Medications = Field(default_factory=Medications)
    taper_plans: list[TaperPlan] = Field(default_factory=list)
    diet_plan: Optional[PlanRef] = None
    exercise_plan: Optional[PlanRef] = None
    advice_blocks: list[str] = Field(default_factory=list)
    charts: list[str] = Field(default_factory=list)
    referrals: list[Referral] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    follow_up: Optional[FollowUp] = None
    certificates: list[CertificateRequest | str] = Field(default_factory=list)
    free_text: Optional[FreeText] = None
    # Filled at render time: template id -> version used (archived with the JSON).
    template_versions: dict[str, str] = Field(default_factory=dict)

    def certificate_requests(self) -> list[CertificateRequest]:
        return [
            c if isinstance(c, CertificateRequest) else CertificateRequest(type=c)
            for c in self.certificates
        ]
