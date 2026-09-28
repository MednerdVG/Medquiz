"""Approve → render → store → deliver → archive (brief §9, §16, §17).

Approval locks the document. Every render is immutable; a correction creates a
new version with an amendment note, never a silent edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth import tokens
from app.config import settings
from app.db import utcnow
from app.delivery import drive
from app.delivery.messaging import notify_patient
from app.models import Booking, Consultation, Patient, PrescriptionRecord, Upload
from app.rx.render.renderer import RenderError, RenderResult, file_name, render
from app.rx.schema import Prescription
from app.rx.suggest import suggest
from app.rx.validate.rules import BLOCK, Issue, blocking, render_issues, unacknowledged, validate
from app.storage import storage


class ApprovalError(Exception):
    def __init__(self, message: str, issues: list[Issue] | None = None, status: int = 409):
        super().__init__(message)
        self.issues, self.status = issues or [], status


@dataclass
class Check:
    issues: list[Issue]
    suggestions: list[dict] = field(default_factory=list)

    @property
    def can_approve(self) -> bool:
        return not blocking(self.issues)


def stored_patient_dict(p: Patient) -> dict:
    return {"name": p.name, "age_years": p.age_years, "sex": p.sex, "patient_id": p.patient_code}


def upload_dicts(db: Session, c: Consultation) -> list[dict]:
    q = select(Upload).where(Upload.deleted_at.is_(None), Upload.detached.is_(False),
                             (Upload.consultation_id == c.id) | ((Upload.booking_id == c.booking_id) & (Upload.booking_id.is_not(None))))
    return [{"id": u.id, "filename": u.filename, "extracted_patient_name": u.extracted_patient_name}
            for u in db.scalars(q).all()]


def check(db: Session, c: Consultation, rx: Prescription) -> Check:
    patient = db.get(Patient, c.patient_id)
    issues = validate(rx, stored_patient=stored_patient_dict(patient), uploads=upload_dicts(db, c))
    if rx.patient.patient_id and rx.patient.patient_id != patient.patient_code:
        issues.append(Issue(BLOCK, "record_mismatch:patient_id",
                            f"Patient ID {rx.patient.patient_id} is not this patient's ID ({patient.patient_code}).",
                            "patient.patient_id"))
    return Check(issues, [s.to_dict() for s in suggest(rx)])


def follow_up_due(rx: Prescription) -> Optional[date]:
    if not rx.follow_up or not rx.follow_up.interval:
        return None
    m = re.search(r"(\d+)\s*(day|week|month|year)", rx.follow_up.interval.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
    return rx.meta.consult_date + timedelta(days=days)


def next_version(db: Session, c: Consultation) -> int:
    return (db.scalar(select(func.max(PrescriptionRecord.version)).where(PrescriptionRecord.consultation_id == c.id)) or 0) + 1


def approve(db: Session, c: Consultation, *, doctor_key: str, email: str, acknowledged: list[str]) -> Optional[PrescriptionRecord]:
    """Returns the new record, or None when waiting for the co-signatory's countersignature."""
    if c.status == "approved":
        raise ApprovalError("Already approved — create an amendment (version 2) to change it.", status=409)
    rx = Prescription.model_validate(c.draft)
    signers = [k for k in (rx.meta.seen_by, rx.meta.co_signatory) if k]
    if doctor_key not in signers:
        raise ApprovalError("Only the treating doctor (or the named co-signatory) can approve — "
                            "a doctor signs only their own block.", status=403)
    rx.meta.version = next_version(db, c)
    res = check(db, c, rx)
    if blocking(res.issues):
        raise ApprovalError("Fix the blocking problems before approving.", res.issues)
    pending = unacknowledged(res.issues, acknowledged)
    if pending:
        raise ApprovalError("Acknowledge each warning to proceed.", pending)
    approvals = [a for a in (c.approvals or []) if a.get("version") == rx.meta.version and a["doctor_key"] != doctor_key]
    approvals.append({"doctor_key": doctor_key, "email": email, "at": utcnow().isoformat(),
                      "acknowledged": sorted(acknowledged), "version": rx.meta.version})
    c.approvals = approvals
    if set(signers) - {a["doctor_key"] for a in approvals}:
        c.status = "awaiting_countersign"
        audit(db, email, "rx.signed_awaiting_countersign", "consultation", c.id, {"version": rx.meta.version})
        db.commit()
        return None

    rx.meta.status = "approved"
    try:
        result = render(rx)
    except RenderError as exc:
        c.status = "draft"
        db.commit()
        raise ApprovalError("The PDF could not be rendered safely.", render_issues(str(exc)))
    rx.meta.status = "rendered"
    rx.template_versions = result.template_versions
    rec = _store(db, c, rx, result, approvers=approvals)
    c.status = "approved"
    c.draft = rx.model_dump(mode="json")
    c.follow_up_due = follow_up_due(rx)
    if c.booking_id:
        b = db.get(Booking, c.booking_id)
        b.status = "completed"
    audit(db, email, "rx.approved", "prescription", rec.id,
          {"consultation_id": c.id, "version": rec.version, "sha256": rec.pdf_sha256, "pages": rec.page_count,
           "acknowledged_warnings": sorted(acknowledged), "template_versions": rx.template_versions})
    db.commit()
    from app.jobs.queue import enqueue
    enqueue(deliver_prescription_job, rec.id)
    db.refresh(rec)
    return rec


def _store(db: Session, c: Consultation, rx: Prescription, result: RenderResult, approvers: list[dict]) -> PrescriptionRecord:
    patient = db.get(Patient, c.patient_id)
    fname = file_name(rx)
    key = f"prescriptions/{patient.patient_code}/{c.id}/v{rx.meta.version}/{fname}"
    storage().put(key, result.pdf, "application/pdf")
    rec = PrescriptionRecord(consultation_id=c.id, patient_id=patient.id, version=rx.meta.version,
                             amendment_note=rx.meta.amendment_note, data=rx.model_dump(mode="json"), pdf_key=key,
                             pdf_sha256=result.sha256, file_name=fname, page_count=result.page_count,
                             approved_by=", ".join(a["email"] for a in approvers),
                             acknowledged_warnings=sorted({w for a in approvers for w in a["acknowledged"]}))
    db.add(rec)
    db.flush()
    return rec


def deliver_prescription_job(record_id: str) -> dict:
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return deliver_prescription(db, db.get(PrescriptionRecord, record_id))
    finally:
        db.close()


def deliver_prescription(db: Session, rec: PrescriptionRecord) -> dict:
    """WhatsApp (PDF attached) + email fallback, Drive copy, link on the patient record."""
    patient = db.get(Patient, rec.patient_id)
    pdf = storage().get(rec.pdf_key)
    portal = tokens.issue(db, kind="portal", patient_id=patient.id, booking_id=None,
                          expires_at=utcnow() + timedelta(days=365))
    portal_url = f"{settings().base_url}/p/{portal}"
    first = patient.name.split()[0] if patient.name else ""
    amended = f" (amended, version {rec.version})" if rec.version > 1 else ""
    body = (f"Hello {first}, here is your Metafix prescription{amended}. "
            f"You can download all your prescriptions any time here: {portal_url}")
    sent = notify_patient(db, patient, kind="prescription", body=body, subject="Your Metafix prescription",
                          pdf=pdf, filename=rec.file_name, attachment_key=rec.pdf_key,
                          dedupe_key=f"prescription:{rec.id}")
    arch = drive.archive_pdf(patient.patient_code, patient.name, rec.file_name, pdf)
    rec.drive_file_id = arch.file_id
    status = {"channels": [{"channel": m.channel, "to": m.to, "status": m.status} for m in sent],
              "drive": {"ok": arch.ok, "path": arch.path, "error": arch.error}}
    rec.delivery_status = status
    audit(db, "system", "rx.delivered", "prescription", rec.id, status)
    db.commit()
    return status


def amend(db: Session, c: Consultation, *, note: str, email: str) -> Consultation:
    if c.status != "approved":
        raise ApprovalError("Only an approved prescription can be amended; edit the draft instead.")
    if not note.strip():
        raise ApprovalError("An amendment note is required.", status=422)
    rx = Prescription.model_validate(c.draft)
    rx.meta.version = next_version(db, c)
    rx.meta.amendment_note = note.strip()
    rx.meta.status = "draft"
    c.draft = rx.model_dump(mode="json")
    c.status = "draft"
    c.approvals = []
    audit(db, email, "rx.amendment_started", "consultation", c.id, {"version": rx.meta.version, "note": note})
    db.commit()
    return c
