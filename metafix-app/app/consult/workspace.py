"""Consultation workspace API: draft autosave, validation, preview, approve, amend, follow-up."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth.deps import (ADMIN, DOCTOR, OWNER, Principal, get_booking_for, get_consultation_for, get_patient_for,
                           require)
from app.bookings.service import aware, tz
from app.config import doctors
from app.db import get_db, new_id
from app.intake.extract import extract_patient_name
from app.intake.forms import ALLOWED_TYPES, MAX_UPLOAD_BYTES, UPLOAD_TAGS, sniff_type
from app.models import Booking, Consultation, IntakeForm, Patient, PrescriptionRecord, Upload
from app.rx import templates as tpl
from app.rx.bmi import bmi_cell
from app.rx.followup import change_summary, clone_for_follow_up, previous_labs_for_comparison
from app.rx.formulary import formulary
from app.rx.render.renderer import RenderError, render
from app.rx.schema import Prescription
from app.rx.service import ApprovalError, amend, approve, check
from app.storage import storage

router = APIRouter(prefix="/api", tags=["consultations"])
STAFF = require(ADMIN, DOCTOR, OWNER)
CLINICIAN = require(DOCTOR, OWNER)


# ---------------------------------------------------------------- draft construction
def _split(text: Optional[str]) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,;]+", text or "") if x.strip()]


def draft_from_intake(db: Session, booking: Booking, doctor_key: str, created_by: str, consultation_id: str) -> dict:
    """Pre-fill identity and patient-reported history only. No clinical content is invented."""
    p = booking.patient
    form = db.scalar(select(IntakeForm).where(IntakeForm.booking_id == booking.id))
    d = (form.data if form else {}) or {}
    v = d.get("vitals") or {}
    strip = []
    for label, key, unit in (("WEIGHT", "weight_kg", "kg"), ("HEIGHT", "height_cm", "cm"), ("WAIST", "waist_cm", "cm")):
        if v.get(key):
            strip.append({"label": label, "value": f"{v[key]:g} {unit}"})
    if v.get("bp"):
        strip.append({"label": "BP", "value": f"{v['bp']} mmHg"})
    if v.get("pulse"):
        strip.append({"label": "PULSE", "value": f"{v['pulse']}/min"})
    meds = [m for m in d.get("current_medicines") or [] if m.get("name")]
    extra = []
    if d.get("city") or p.city:
        extra.append({"label": "Resident of", "value": d.get("city") or p.city})
    if meds:
        extra.append({"label": "Current medicines (patient-reported)",
                      "value": " · ".join(" ".join(x for x in [m["name"], m.get("dose"), m.get("timing")] if x) for m in meds)})
    diet = {"veg": "vegetarian", "jain": "Jain", "eggetarian": "eggetarian"}.get(d.get("diet_type"), d.get("diet_type"))
    return {
        "meta": {"consultation_id": consultation_id, "booking_id": booking.id,
                 "consult_date": aware(booking.slot_start).astimezone(tz()).date().isoformat(),
                 "consult_type": "teleconsult", "visit_number": 1, "seen_by": doctor_key, "created_by": created_by},
        "patient": {"name": d.get("name") or p.name, "age_years": d.get("age") or p.age_years,
                    "sex": d.get("sex") or p.sex, "patient_id": p.patient_code, "diet_type": diet,
                    "allergies": _split(d.get("allergies")), "known_conditions": _split(d.get("known_conditions")),
                    "family_history": _split(d.get("family_history")), "extra_rows": extra},
        "vitals": {"weight_kg": v.get("weight_kg"), "height_cm": v.get("height_cm"), "waist_cm": v.get("waist_cm"),
                   "pulse": v.get("pulse"), "strip": strip},
        "complaints": [{"text": t} for t in _split(d.get("complaints"))][:8],
    }


def last_approved(db: Session, patient_id: str, exclude: Optional[str] = None) -> Optional[PrescriptionRecord]:
    stmt = select(PrescriptionRecord).where(PrescriptionRecord.patient_id == patient_id,
                                            PrescriptionRecord.deleted_at.is_(None))
    if exclude:
        stmt = stmt.where(PrescriptionRecord.consultation_id != exclude)
    return db.scalar(stmt.order_by(PrescriptionRecord.approved_at.desc()))


def get_or_create_consultation(db: Session, booking: Booking, p: Principal, *, from_last: bool = False) -> Consultation:
    c = db.scalar(select(Consultation).where(Consultation.booking_id == booking.id, Consultation.deleted_at.is_(None)))
    if c:
        return c
    doctor_key = booking.assigned_doctor or p.doctor_key
    cid = new_id()
    prev = last_approved(db, booking.patient_id)
    if from_last and prev:
        rx = clone_for_follow_up(Prescription.model_validate(prev.data),
                                 consult_date=aware(booking.slot_start).astimezone(tz()).date(),
                                 consultation_id=cid, booking_id=booking.id, seen_by=doctor_key)
        draft = rx.model_dump(mode="json")
    else:
        draft = draft_from_intake(db, booking, doctor_key, p.email, cid)
        if prev:
            draft["meta"]["visit_number"] = (prev.data.get("meta", {}).get("visit_number") or 1) + 1
            draft["meta"]["previous_consultation_id"] = prev.consultation_id
        draft = Prescription.model_validate(draft).model_dump(mode="json")
    c = Consultation(id=cid, patient_id=booking.patient_id, booking_id=booking.id, doctor_key=doctor_key,
                     previous_consultation_id=prev.consultation_id if prev else None, draft=draft)
    db.add(c)
    db.flush()
    # uploads made for this booking belong to the consultation
    for u in db.scalars(select(Upload).where(Upload.booking_id == booking.id, Upload.consultation_id.is_(None))):
        u.consultation_id = c.id
    audit(db, p.email, "consult.created", "consultation", c.id, {"booking_id": booking.id, "from_last": bool(from_last and prev)})
    return c


# ---------------------------------------------------------------- payloads
def _upload_row(u: Upload) -> dict:
    return {"id": u.id, "filename": u.filename, "tag": u.tag, "tag_label": UPLOAD_TAGS.get(u.tag, u.tag),
            "content_type": u.content_type, "report_date": u.report_date.isoformat() if u.report_date else None,
            "uploaded_by": u.uploaded_by, "extracted_patient_name": u.extracted_patient_name, "detached": u.detached,
            "url": storage().signed_url(u.storage_key, 900, u.filename)}


def workspace_payload(db: Session, c: Consultation) -> dict:
    patient = db.get(Patient, c.patient_id)
    booking = db.get(Booking, c.booking_id) if c.booking_id else None
    intake = db.scalar(select(IntakeForm).where(IntakeForm.booking_id == c.booking_id)) if c.booking_id else None
    uploads = db.scalars(select(Upload).where(Upload.patient_id == patient.id, Upload.deleted_at.is_(None))
                         .order_by(Upload.created_at.desc())).all()
    history = db.scalars(select(PrescriptionRecord).where(PrescriptionRecord.patient_id == patient.id,
                                                          PrescriptionRecord.deleted_at.is_(None))
                         .order_by(PrescriptionRecord.approved_at.desc())).all()
    try:
        rx = Prescription.model_validate(c.draft)
        chk = check(db, c, rx)
        issues, suggestions, schema_error = [i.to_dict() for i in chk.issues], chk.suggestions, None
    except ValidationError as exc:
        issues, suggestions, schema_error = [], [], str(exc)
    return {
        "consultation": {"id": c.id, "status": c.status, "doctor_key": c.doctor_key, "approvals": c.approvals or [],
                         "previous_consultation_id": c.previous_consultation_id, "change_summary": c.change_summary},
        "draft": c.draft, "issues": issues, "suggestions": suggestions, "schema_error": schema_error,
        "patient": {"id": patient.id, "code": patient.patient_code, "name": patient.name, "phone": patient.phone,
                    "email": patient.email, "age_years": patient.age_years, "sex": patient.sex, "city": patient.city},
        "booking": {"id": booking.id, "slot_start": aware(booking.slot_start).isoformat(), "meet_link": booking.meet_link,
                    "service": booking.service, "notes": booking.notes_from_booking} if booking else None,
        "intake": {"data": intake.data if intake else {}, "consent_given_at":
                   intake.consent_given_at.isoformat() if intake and intake.consent_given_at else None},
        "uploads": [_upload_row(u) for u in uploads],
        "history": [{"id": r.id, "consultation_id": r.consultation_id, "version": r.version, "file_name": r.file_name,
                     "approved_at": r.approved_at.isoformat(), "approved_by": r.approved_by,
                     "url": storage().signed_url(r.pdf_key, 900, r.file_name)} for r in history],
        "doctors": {k: {"name": d.sign_name} for k, d in doctors().items()},
    }


# ---------------------------------------------------------------- routes
@router.get("/consultations/{cid}")
def get_workspace(cid: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    c = get_consultation_for(db, cid, p)
    audit(db, p.email, "consult.viewed", "consultation", c.id, commit=True)
    return workspace_payload(db, c)


class NewConsultIn(BaseModel):
    booking_id: str
    from_last: bool = False


@router.post("/consultations")
def create_consultation(body: NewConsultIn, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    b = get_booking_for(db, body.booking_id, p)
    c = get_or_create_consultation(db, b, p, from_last=body.from_last)
    db.commit()
    return workspace_payload(db, c)


class FollowUpIn(BaseModel):
    booking_id: Optional[str] = None
    consult_date: Optional[date] = None


@router.post("/consultations/{cid}/follow-up")
def new_visit_from_last(cid: str, body: FollowUpIn, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    """'New visit from last consultation': clone everything, mark follow-up, summarise changes."""
    prev_c = get_consultation_for(db, cid, p)
    rec = db.scalar(select(PrescriptionRecord).where(PrescriptionRecord.consultation_id == prev_c.id)
                    .order_by(PrescriptionRecord.version.desc()))
    if not rec:
        raise HTTPException(409, "The last consultation has no approved prescription to start from")
    booking = get_booking_for(db, body.booking_id, p) if body.booking_id else None
    if booking and db.scalar(select(Consultation.id).where(Consultation.booking_id == booking.id, Consultation.deleted_at.is_(None))):
        raise HTTPException(409, "That booking already has a consultation")
    new_cid = new_id()
    rx = clone_for_follow_up(Prescription.model_validate(rec.data), consult_date=body.consult_date or date.today(),
                             consultation_id=new_cid, booking_id=booking.id if booking else None,
                             seen_by=(booking.assigned_doctor if booking else None) or p.doctor_key or prev_c.doctor_key)
    c = Consultation(id=new_cid, patient_id=prev_c.patient_id, booking_id=booking.id if booking else None,
                     doctor_key=rx.meta.seen_by, previous_consultation_id=prev_c.id, draft=rx.model_dump(mode="json"))
    db.add(c)
    audit(db, p.email, "consult.follow_up_cloned", "consultation", new_cid, {"from": prev_c.id, "version": rec.version})
    db.commit()
    return workspace_payload(db, c)


@router.put("/consultations/{cid}/draft")
def save_draft(cid: str, draft: dict, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    c = get_consultation_for(db, cid, p)
    if c.status == "approved":
        raise HTTPException(409, "Approved prescriptions are locked — start an amendment to change it")
    try:
        rx = Prescription.model_validate(draft)
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": ".".join(map(str, e["loc"])), "msg": e["msg"]} for e in exc.errors()])
    if rx.meta.consultation_id != c.id:
        raise HTTPException(422, "Draft belongs to a different consultation")
    if rx.meta.seen_by and rx.meta.seen_by not in doctors():
        raise HTTPException(422, "Unknown doctor in 'seen by'")
    stored = Prescription.model_validate(c.draft) if c.draft else None
    if stored:  # server-owned fields
        rx.meta.version, rx.meta.amendment_note, rx.meta.status = stored.meta.version, stored.meta.amendment_note, "draft"
    c.draft = rx.model_dump(mode="json")
    if c.status == "awaiting_countersign":
        c.status, c.approvals = "draft", []  # any edit voids pending sign-offs
    audit(db, p.email, "consult.draft_saved", "consultation", c.id)
    db.commit()
    chk = check(db, c, rx)
    return {"saved": True, "issues": [i.to_dict() for i in chk.issues], "suggestions": chk.suggestions}


@router.post("/consultations/{cid}/validate")
def validate_draft(cid: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    c = get_consultation_for(db, cid, p)
    rx = Prescription.model_validate(c.draft)
    chk = check(db, c, rx)
    return {"issues": [i.to_dict() for i in chk.issues], "suggestions": chk.suggestions, "can_approve": chk.can_approve}


@router.get("/consultations/{cid}/preview.pdf")
def preview(cid: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    c = get_consultation_for(db, cid, p)
    rx = Prescription.model_validate(c.draft)
    try:
        res = render(rx)
    except RenderError as exc:
        raise HTTPException(409, str(exc))
    audit(db, p.email, "rx.previewed", "consultation", c.id, {"pages": res.page_count}, commit=True)
    return Response(res.pdf, media_type="application/pdf", headers={"Content-Disposition": "inline; filename=preview.pdf",
                                                                    "Cache-Control": "no-store"})


class ApproveIn(BaseModel):
    acknowledged: list[str] = []


def _approval_http(exc: ApprovalError):
    raise HTTPException(exc.status, {"message": str(exc), "issues": [i.to_dict() for i in exc.issues]})


@router.post("/consultations/{cid}/approve")
def approve_route(cid: str, body: ApproveIn, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    c = get_consultation_for_signing(db, cid, p)
    try:
        rec = approve(db, c, doctor_key=p.doctor_key, email=p.email, acknowledged=body.acknowledged)
    except ApprovalError as exc:
        _approval_http(exc)
    if rec is None:
        return {"status": "awaiting_countersign"}
    return {"status": "approved", "prescription_id": rec.id, "version": rec.version, "file_name": rec.file_name,
            "pages": rec.page_count, "sha256": rec.pdf_sha256, "delivery": rec.delivery_status,
            "url": storage().signed_url(rec.pdf_key, 900, rec.file_name)}


def get_consultation_for_signing(db: Session, cid: str, p: Principal) -> Consultation:
    """The co-signatory can open a consultation to countersign even if it is not assigned to them."""
    c = db.get(Consultation, cid)
    if c and not c.deleted_at and p.doctor_key and (c.draft.get("meta") or {}).get("co_signatory") == p.doctor_key:
        return c
    return get_consultation_for(db, cid, p)


class AmendIn(BaseModel):
    note: str


@router.post("/consultations/{cid}/amend")
def amend_route(cid: str, body: AmendIn, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    c = get_consultation_for(db, cid, p)
    try:
        amend(db, c, note=body.note, email=p.email)
    except ApprovalError as exc:
        _approval_http(exc)
    return workspace_payload(db, c)


@router.get("/consultations/{cid}/change-summary")
def get_change_summary(cid: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    c = get_consultation_for(db, cid, p)
    if not c.previous_consultation_id:
        return {"available": False}
    prev = db.scalar(select(PrescriptionRecord).where(PrescriptionRecord.consultation_id == c.previous_consultation_id)
                     .order_by(PrescriptionRecord.version.desc()))
    if not prev:
        return {"available": False}
    cs = change_summary(Prescription.model_validate(prev.data), Prescription.model_validate(c.draft))
    c.change_summary = cs.to_dict()
    db.commit()
    return {"available": True, **cs.to_dict()}


@router.post("/consultations/{cid}/change-summary/insert")
def insert_change_summary(cid: str, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    """Drop the change summary into the document as the interval_review section."""
    c = get_consultation_for(db, cid, p)
    summary = get_change_summary(cid, db, p)
    if not summary.get("available"):
        raise HTTPException(409, "No previous consultation to compare with")
    draft = dict(c.draft)
    draft["interval_review"] = list(draft.get("interval_review") or []) + summary["interval_review_lines"]
    c.draft = Prescription.model_validate(draft).model_dump(mode="json")
    audit(db, p.email, "consult.change_summary_inserted", "consultation", c.id)
    db.commit()
    return workspace_payload(db, c)


@router.post("/consultations/{cid}/pull-labs")
def pull_labs(cid: str, db: Session = Depends(get_db), p: Principal = Depends(CLINICIAN)):
    c = get_consultation_for(db, cid, p)
    if not c.previous_consultation_id:
        raise HTTPException(409, "No previous consultation")
    prev = db.scalar(select(PrescriptionRecord).where(PrescriptionRecord.consultation_id == c.previous_consultation_id)
                     .order_by(PrescriptionRecord.version.desc()))
    rows = previous_labs_for_comparison(Prescription.model_validate(prev.data)) if prev else []
    return {"labs": rows}


@router.post("/consultations/{cid}/uploads")
async def staff_upload(cid: str, file: UploadFile = File(...), tag: str = Form("other"),
                       report_date: Optional[str] = Form(None), db: Session = Depends(get_db),
                       p: Principal = Depends(STAFF)):
    c = get_consultation_for(db, cid, p)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Each file can be up to 25 MB")
    ctype = sniff_type(data[:32], file.content_type or "")
    if ctype not in ALLOWED_TYPES or tag not in UPLOAD_TAGS:
        raise HTTPException(415, "JPEG, PNG, HEIC or PDF only, with a valid tag")
    uid = new_id()
    key = f"uploads/{c.patient_id}/{uid}{ALLOWED_TYPES[ctype]}"
    storage().put(key, data, ctype)
    db.add(Upload(id=uid, patient_id=c.patient_id, booking_id=c.booking_id, consultation_id=c.id, storage_key=key,
                  filename=(file.filename or "upload")[:200], content_type=ctype, size_bytes=len(data),
                  sha256=hashlib.sha256(data).hexdigest(), tag=tag,
                  report_date=date.fromisoformat(report_date) if report_date else None, uploaded_by=p.email,
                  extracted_patient_name=extract_patient_name(data, ctype)))
    audit(db, p.email, "upload.added", "upload", uid, {"consultation_id": c.id})
    db.commit()
    return workspace_payload(db, c)


class DetachIn(BaseModel):
    detached: bool = True


@router.post("/uploads/{upload_id}/detach")
def detach_upload(upload_id: str, body: DetachIn, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    u = db.get(Upload, upload_id)
    if not u or u.deleted_at:
        raise HTTPException(404)
    get_patient_for(db, u.patient_id, p)
    u.detached = body.detached
    audit(db, p.email, "upload.detached" if body.detached else "upload.reattached", "upload", u.id, commit=True)
    return {"id": u.id, "detached": u.detached}


# ---------------------------------------------------------------- form helpers (autocomplete, libraries)
@router.get("/formulary/search")
def formulary_search(q: str, p: Principal = Depends(STAFF)):
    f = formulary()
    ql = q.lower().strip()
    out = []
    for brand, info in f["brands"].items():
        if brand.lower().startswith(ql) or ql in brand.lower():
            out.append({"brand": brand, "generic": " + ".join(info.get("molecules", [])),
                        "strengths": info.get("strengths") or [], "schedule": info.get("schedule")})
    for mol, info in f["molecules"].items():
        if mol.startswith(ql):
            out.append({"brand": mol.capitalize(), "generic": mol, "strengths": info.get("strengths") or [],
                        "schedule": info.get("schedule"), "is_generic": True})
    return sorted(out, key=lambda r: (not r["brand"].lower().startswith(ql), r["brand"]))[:20]


@router.get("/diagnoses/search")
def diagnosis_search(q: str = "", db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    """Autocomplete from the clinic's own past diagnoses (never a suggestion engine)."""
    counts: dict[str, int] = {}
    for data in db.scalars(select(PrescriptionRecord.data).where(PrescriptionRecord.deleted_at.is_(None))).all():
        for d in data.get("diagnoses") or []:
            t = d.get("text", "").strip()
            if t and q.lower() in t.lower():
                counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:15]]


@router.get("/templates/{kind}")
def template_list(kind: str, p: Principal = Depends(STAFF)):
    if kind not in tpl.KINDS:
        raise HTTPException(404)
    return tpl.list_templates(kind)


@router.get("/templates/{kind}/{tid}")
def template_body(kind: str, tid: str, p: Principal = Depends(STAFF)):
    try:
        return tpl.load(kind, tid)
    except (tpl.TemplateNotFound, ValueError):
        raise HTTPException(404)


@router.get("/investigations/library")
def investigation_library(p: Principal = Depends(STAFF)):
    with open(tpl.TEMPLATES_DIR / "investigations.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@router.get("/bmi")
def bmi(weight_kg: float, height_cm: float, p: Principal = Depends(STAFF)):
    return bmi_cell(weight_kg, height_cm) or {}
