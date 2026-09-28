"""Patients, doctors & availability, settings, audit log, DPDP deletion, doctor dashboard."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.app_settings import DEFAULTS, get_setting, set_setting
from app.audit import audit
from app.auth.deps import ADMIN, DOCTOR, OWNER, Principal, get_patient_for, require, scope_bookings, scope_patients
from app.bookings.routes import booking_row
from app.bookings.service import aware, tz
from app.config import doctors
from app.db import get_db, utcnow
from app.models import (AccessToken, AuditLog, Availability, AvailabilityException, Booking, Consultation, IntakeForm,
                        Message, Patient, PrescriptionRecord, Upload, User)
from app.storage import storage

router = APIRouter(prefix="/api", tags=["admin"])
STAFF = require(ADMIN, DOCTOR, OWNER)


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    """Today / Upcoming / Awaiting prescription / Completed — only the caller's own for doctors."""
    now = datetime.now(tz())
    start_today = datetime.combine(now.date(), time(0), tz())
    end_today = start_today + timedelta(days=1)
    rows = db.scalars(scope_bookings(select(Booking), p).where(Booking.status != "cancelled")
                      .order_by(Booking.slot_start)).all()
    out = {"today": [], "upcoming": [], "awaiting_prescription": [], "completed": []}
    for b in rows:
        if p.sees_all and not b.assigned_doctor:
            continue  # unassigned ones live in the bookings inbox
        r = booking_row(db, b)
        s = aware(b.slot_start)
        if b.status == "completed":
            out["completed"].append(r)
        elif aware(b.slot_end) < now:
            out["awaiting_prescription"].append(r)
        elif start_today <= s < end_today:
            out["today"].append(r)
        elif s >= end_today:
            out["upcoming"].append(r)
    out["completed"] = out["completed"][::-1][:50]
    return out


# ---------------------------------------------------------------- patients
@router.get("/patients")
def list_patients(q: str = "", db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    stmt = scope_patients(select(Patient), p)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Patient.name).like(like), Patient.phone.like(f"%{q}%"),
                              func.lower(Patient.patient_code).like(like)))
    return [{"id": x.id, "code": x.patient_code, "name": x.name, "phone": x.phone, "email": x.email,
             "age_years": x.age_years, "sex": x.sex} for x in db.scalars(stmt.order_by(Patient.created_at.desc()).limit(200))]


@router.get("/patients/{patient_id}")
def patient_detail(patient_id: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    pt = get_patient_for(db, patient_id, p)
    bookings = db.scalars(scope_bookings(select(Booking).where(Booking.patient_id == pt.id), p)
                          .order_by(Booking.slot_start.desc())).all()
    cstmt = select(Consultation).where(Consultation.patient_id == pt.id, Consultation.deleted_at.is_(None))
    if not p.sees_all:
        cstmt = cstmt.where(Consultation.doctor_key == p.doctor_key)
    consults = db.scalars(cstmt.order_by(Consultation.created_at.desc())).all()
    cids = [c.id for c in consults]
    rxs = db.scalars(select(PrescriptionRecord).where(PrescriptionRecord.consultation_id.in_(cids),
                                                      PrescriptionRecord.deleted_at.is_(None))
                     .order_by(PrescriptionRecord.approved_at.desc())).all() if cids else []
    audit(db, p.email, "patient.viewed", "patient", pt.id, commit=True)
    return {"patient": {"id": pt.id, "code": pt.patient_code, "name": pt.name, "phone": pt.phone, "email": pt.email,
                        "age_years": pt.age_years, "sex": pt.sex, "city": pt.city},
            "bookings": [booking_row(db, b) for b in bookings],
            "consultations": [{"id": c.id, "status": c.status, "doctor_key": c.doctor_key,
                               "created_at": c.created_at.isoformat(),
                               "consult_date": (c.draft.get("meta") or {}).get("consult_date")} for c in consults],
            "prescriptions": [{"id": r.id, "consultation_id": r.consultation_id, "version": r.version,
                               "file_name": r.file_name, "approved_at": r.approved_at.isoformat(),
                               "approved_by": r.approved_by, "amendment_note": r.amendment_note,
                               "delivery": r.delivery_status, "drive_file_id": r.drive_file_id,
                               "url": storage().signed_url(r.pdf_key, 900, r.file_name)} for r in rxs]}


class PatientPatch(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    age_years: Optional[int] = None
    sex: Optional[str] = None
    city: Optional[str] = None


@router.patch("/patients/{patient_id}")
def update_patient(patient_id: str, body: PatientPatch, db: Session = Depends(get_db),
                   p: Principal = Depends(require(ADMIN, OWNER))):
    pt = get_patient_for(db, patient_id, p)
    changes = body.model_dump(exclude_unset=True)
    if "phone" in changes and changes["phone"]:
        from app.bookings.adapters.base import normalise_phone
        changes["phone"] = normalise_phone(changes["phone"])
    before = {k: getattr(pt, k) for k in changes}
    for k, v in changes.items():
        setattr(pt, k, v)
    audit(db, p.email, "patient.updated", "patient", pt.id, {"before": before, "after": changes}, commit=True)
    return {"ok": True}


class DeleteIn(BaseModel):
    confirm_code: str
    reason: str


@router.post("/patients/{patient_id}/erase")
def erase_patient(patient_id: str, body: DeleteIn, db: Session = Depends(get_db), p: Principal = Depends(require(OWNER))):
    """DPDP Act 2023 deletion on request: hard delete of personal data and files. Explicit, owner-only, audited.

    The audit rows keep only ids and the reason. Note: Telemedicine Practice Guidelines retention may require
    keeping the prescription itself; the owner decides before confirming.
    """
    pt = db.get(Patient, patient_id)
    if not pt:
        raise HTTPException(404)
    if body.confirm_code != pt.patient_code:
        raise HTTPException(422, "Type the patient ID to confirm")
    keys = [u.storage_key for u in db.scalars(select(Upload).where(Upload.patient_id == pt.id))] + \
           [r.pdf_key for r in db.scalars(select(PrescriptionRecord).where(PrescriptionRecord.patient_id == pt.id))]
    for k in keys:
        try:
            storage().delete(k)
        except Exception:
            pass
    booking_ids = [b.id for b in db.scalars(select(Booking).where(Booking.patient_id == pt.id))]
    for model, col in ((Message, Message.patient_id), (AccessToken, AccessToken.patient_id), (Upload, Upload.patient_id),
                       (PrescriptionRecord, PrescriptionRecord.patient_id)):
        for row in db.scalars(select(model).where(col == pt.id)):
            db.delete(row)
    db.flush()
    for c in db.scalars(select(Consultation).where(Consultation.patient_id == pt.id)):
        db.delete(c)
    for f in db.scalars(select(IntakeForm).where(IntakeForm.booking_id.in_(booking_ids))):
        db.delete(f)
    db.flush()
    for b in db.scalars(select(Booking).where(Booking.patient_id == pt.id)):
        db.delete(b)
    db.flush()
    code = pt.patient_code
    db.delete(pt)
    audit(db, p.email, "patient.erased", "patient", patient_id, {"patient_code": code, "reason": body.reason,
                                                                 "files_deleted": len(keys)})
    db.commit()
    return {"erased": True, "files_deleted": len(keys)}


# ---------------------------------------------------------------- doctors, users, availability
@router.get("/doctors")
def list_doctors(db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    users = {u.doctor_key: u for u in db.scalars(select(User).where(User.doctor_key.is_not(None), User.deleted_at.is_(None)))}
    return [{"key": k, "name": d.sign_name, "degrees": d.lh_degrees, "registration_no": d.registration_no,
             "sign_style": d.sign_style, "calendar_connected": bool(users.get(k) and users[k].calendar_id),
             "email": users[k].email if k in users else None} for k, d in doctors().items()]


class UserIn(BaseModel):
    email: str
    name: str
    role: str
    doctor_key: Optional[str] = None
    active: bool = True


@router.get("/users")
def list_users(db: Session = Depends(get_db), p: Principal = Depends(require(OWNER))):
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "doctor_key": u.doctor_key,
             "active": u.active, "has_2fa": bool(u.totp_secret), "calendar_connected": bool(u.calendar_id)}
            for u in db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.email))]


@router.post("/users")
def upsert_user(body: UserIn, db: Session = Depends(get_db), p: Principal = Depends(require(OWNER))):
    if body.role not in (ADMIN, DOCTOR, OWNER):
        raise HTTPException(422, "role must be admin, doctor or owner")
    if body.doctor_key and body.doctor_key not in doctors():
        raise HTTPException(422, "doctor_key must exist in doctors.yaml")
    u = db.scalar(select(User).where(User.email == body.email.lower()))
    if not u:
        u = User(email=body.email.lower(), name=body.name, role=body.role)
        db.add(u)
    u.name, u.role, u.doctor_key, u.active, u.deleted_at = body.name, body.role, body.doctor_key, body.active, None
    audit(db, p.email, "user.saved", "user", u.email, body.model_dump())
    db.commit()
    return {"ok": True}


class WindowIn(BaseModel):
    weekday: int
    start: time
    end: time


class ExceptionIn(BaseModel):
    day: date
    start: Optional[time] = None
    end: Optional[time] = None
    available: bool = False
    note: Optional[str] = None


def _own_or_admin(p: Principal, doctor_key: str):
    if doctor_key not in doctors():
        raise HTTPException(404)
    if not p.sees_all and p.doctor_key != doctor_key:
        raise HTTPException(403, "You can only edit your own availability")


@router.get("/availability/{doctor_key}")
def get_availability(doctor_key: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    _own_or_admin(p, doctor_key)
    w = db.scalars(select(Availability).where(Availability.doctor_key == doctor_key, Availability.deleted_at.is_(None))
                   .order_by(Availability.weekday, Availability.start)).all()
    ex = db.scalars(select(AvailabilityException).where(AvailabilityException.doctor_key == doctor_key,
                                                        AvailabilityException.deleted_at.is_(None),
                                                        AvailabilityException.day >= date.today())
                    .order_by(AvailabilityException.day)).all()
    return {"weekly": [{"id": x.id, "weekday": x.weekday, "start": x.start.isoformat("minutes"), "end": x.end.isoformat("minutes")} for x in w],
            "exceptions": [{"id": x.id, "day": x.day.isoformat(), "start": x.start.isoformat("minutes") if x.start else None,
                            "end": x.end.isoformat("minutes") if x.end else None, "available": x.available, "note": x.note} for x in ex]}


@router.put("/availability/{doctor_key}/weekly")
def set_weekly(doctor_key: str, windows: list[WindowIn], db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    _own_or_admin(p, doctor_key)
    for row in db.scalars(select(Availability).where(Availability.doctor_key == doctor_key, Availability.deleted_at.is_(None))):
        row.deleted_at = utcnow()
    for w in windows:
        if not (0 <= w.weekday <= 6) or w.end <= w.start:
            raise HTTPException(422, "Each window needs a weekday 0–6 and an end after its start")
        db.add(Availability(doctor_key=doctor_key, weekday=w.weekday, start=w.start, end=w.end))
    audit(db, p.email, "availability.weekly_set", "doctor", doctor_key, {"windows": len(windows)})
    db.commit()
    return get_availability(doctor_key, db, p)


@router.post("/availability/{doctor_key}/exceptions")
def add_exception(doctor_key: str, body: ExceptionIn, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    _own_or_admin(p, doctor_key)
    db.add(AvailabilityException(doctor_key=doctor_key, **body.model_dump()))
    audit(db, p.email, "availability.exception_added", "doctor", doctor_key, body.model_dump(mode="json"))
    db.commit()
    return get_availability(doctor_key, db, p)


@router.delete("/availability/{doctor_key}/exceptions/{ex_id}")
def delete_exception(doctor_key: str, ex_id: str, db: Session = Depends(get_db), p: Principal = Depends(STAFF)):
    _own_or_admin(p, doctor_key)
    ex = db.get(AvailabilityException, ex_id)
    if not ex or ex.doctor_key != doctor_key:
        raise HTTPException(404)
    ex.deleted_at = utcnow()
    audit(db, p.email, "availability.exception_removed", "doctor", doctor_key, commit=True)
    return get_availability(doctor_key, db, p)


# ---------------------------------------------------------------- settings / audit
@router.get("/settings")
def get_settings(db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    return {k: get_setting(db, k) for k in DEFAULTS}


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    allowed_sources = {"superprofile_webhook", "zapier", "email_parsed", "manual"}
    for k, v in body.items():
        if k not in DEFAULTS:
            raise HTTPException(422, f"unknown setting {k}")
        if k == "booking_sources" and (not isinstance(v, list) or not set(v) <= allowed_sources):
            raise HTTPException(422, "booking_sources must be a list of known sources")
        if k == "send_intake_on" and v not in ("assignment", "booking"):
            raise HTTPException(422, "send_intake_on must be 'assignment' or 'booking'")
        set_setting(db, k, v)
    audit(db, p.email, "settings.updated", "settings", None, body)
    db.commit()
    return get_settings(db, p)


@router.get("/audit")
def audit_log(entity_id: Optional[str] = None, actor: Optional[str] = None, limit: int = 200,
              db: Session = Depends(get_db), p: Principal = Depends(require(OWNER))):
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    return [{"at": a.at.isoformat(), "actor": a.actor, "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "detail": a.detail, "ip": a.ip} for a in db.scalars(stmt)]
