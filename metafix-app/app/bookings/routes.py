"""Booking intake endpoints (webhook, bridge, email, manual) and the admin inbox API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_settings import get_setting
from app.audit import audit
from app.auth.deps import ADMIN, DOCTOR, OWNER, Principal, get_booking_for, require, scope_bookings
from app.bookings import service
from app.bookings.adapters.base import verify_hmac
from app.bookings.adapters.email_parser import EmailParserAdapter
from app.bookings.adapters.manual import ManualAdapter, ManualBookingIn
from app.bookings.adapters.superprofile_webhook import SuperprofileWebhookAdapter
from app.bookings.adapters.zapier import ZapierAdapter
from app.config import doctors, settings
from app.consult.meet import ensure_meet, set_manual_link
from app.db import get_db
from app.models import Booking, Consultation, IntakeForm, Upload
from app.intake.forms import completeness

webhooks = APIRouter(prefix="/webhooks/bookings", tags=["webhooks"])
api = APIRouter(prefix="/api/bookings", tags=["bookings"])


def _enabled(db: Session, source: str) -> None:
    if source not in (get_setting(db, "booking_sources") or []):
        raise HTTPException(409, f"Booking source '{source}' is switched off in settings")


async def _verified_json(request: Request) -> dict:
    body = await request.body()
    sig = request.headers.get("x-signature") or request.headers.get("x-hub-signature-256") \
        or request.headers.get("x-superprofile-signature")
    if not verify_hmac(settings().webhook_secret, body, sig):
        raise HTTPException(401, "Bad signature")
    try:
        return json.loads(body)
    except ValueError:
        raise HTTPException(400, "Body is not JSON")


def _result(r: service.IngestResult) -> dict:
    return {"booking_id": r.booking.id, "external_id": r.booking.external_id, "created": r.created,
            "patient_id": r.booking.patient_id, "patient_created": r.patient_created, "notes": r.notes}


@webhooks.post("/superprofile")
async def superprofile_webhook(request: Request, db: Session = Depends(get_db)):
    """Native webhook — and the automation bridge, which posts the normalised JSON to the same URL."""
    payload = await _verified_json(request)
    is_normalised = {"external_id", "slot_start", "patient"} <= payload.keys()
    source = "zapier" if is_normalised else "superprofile_webhook"
    _enabled(db, source)
    try:
        nb = (ZapierAdapter() if is_normalised else SuperprofileWebhookAdapter()).parse(payload)
    except (ValueError, KeyError) as exc:
        audit(db, f"webhook:{source}", "booking.rejected", "booking", None, {"error": str(exc)}, commit=True)
        raise HTTPException(422, f"Could not read booking: {exc}")
    return _result(service.ingest(db, nb, actor=f"webhook:{source}"))


@webhooks.post("/bridge")
async def bridge_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await _verified_json(request)
    _enabled(db, "zapier")
    try:
        nb = ZapierAdapter().parse(payload)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, f"Could not read booking: {exc}")
    return _result(service.ingest(db, nb, actor="webhook:zapier"))


@webhooks.post("/email")
async def email_webhook(request: Request, db: Session = Depends(get_db)):
    """Inbound-mail hook for the dedicated mailbox (raw RFC 822 message or plain text body)."""
    body = await request.body()
    if not verify_hmac(settings().webhook_secret, body, request.headers.get("x-signature")):
        raise HTTPException(401, "Bad signature")
    _enabled(db, "email_parsed")
    try:
        nb = EmailParserAdapter().parse(body.decode("utf-8", "replace"))
    except ValueError as exc:
        audit(db, "webhook:email", "booking.email_unparsed", "booking", None, {"error": str(exc)}, commit=True)
        raise HTTPException(422, f"Could not parse the confirmation email: {exc}")
    return _result(service.ingest(db, nb, actor="webhook:email"))


# ---------------------------------------------------------------- admin / doctor API
def booking_row(db: Session, b: Booking) -> dict:
    intake = db.scalar(select(IntakeForm).where(IntakeForm.booking_id == b.id))
    n_uploads = len(db.scalars(select(Upload.id).where(Upload.booking_id == b.id, Upload.deleted_at.is_(None),
                                                       Upload.detached.is_(False))).all())
    pct = completeness(intake.data if intake else {}, bool(intake and intake.consent_given_at))
    consult = db.scalar(select(Consultation).where(Consultation.booking_id == b.id, Consultation.deleted_at.is_(None)))
    return {
        "id": b.id, "external_id": b.external_id, "source": b.source, "needs_review": b.needs_review,
        "slot_start": service.aware(b.slot_start).isoformat(), "slot_end": service.aware(b.slot_end).isoformat(),
        "slot_label": service.fmt_slot(b),
        "service": b.service, "amount_paid": b.amount_paid, "currency": b.currency, "payment_status": b.payment_status,
        "status": b.status, "assigned_doctor": b.assigned_doctor,
        "assigned_doctor_name": doctors()[b.assigned_doctor].sign_name if b.assigned_doctor in doctors() else None,
        "patient": {"id": b.patient.id, "code": b.patient.patient_code, "name": b.patient.name,
                    "phone": b.patient.phone, "email": b.patient.email},
        "meet_link": b.meet_link, "meet_link_manual": b.meet_link_manual,
        "intake_percent": pct, "uploads": n_uploads,
        "intake_badge": f"Intake {pct}% · {n_uploads} report{'s' if n_uploads != 1 else ''}",
        "consultation_id": consult.id if consult else None,
        "consultation_status": consult.status if consult else None,
    }


@api.get("")
def list_bookings(status: Optional[str] = None, doctor: Optional[str] = None, db: Session = Depends(get_db),
                  p: Principal = Depends(require(ADMIN, DOCTOR, OWNER))):
    stmt = scope_bookings(select(Booking), p)
    if status:
        stmt = stmt.where(Booking.status == status)
    if doctor and p.sees_all:
        stmt = stmt.where(Booking.assigned_doctor == doctor)
    order = Booking.slot_start if p.role == DOCTOR else Booking.created_at.desc()
    return [booking_row(db, b) for b in db.scalars(stmt.order_by(order)).all()]


@api.get("/{booking_id}")
def get_booking(booking_id: str, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, DOCTOR, OWNER))):
    b = get_booking_for(db, booking_id, p)
    audit(db, p.email, "booking.viewed", "booking", b.id, commit=True)
    return booking_row(db, b)


@api.post("")
def create_manual(data: ManualBookingIn, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    nb = ManualAdapter().parse(data)
    return _result(service.ingest(db, nb, actor=p.email))


class PasteIn(BaseModel):
    text: str


@api.post("/paste")
def create_from_paste(body: PasteIn, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    try:
        nb = ManualAdapter().parse_pasted(body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _result(service.ingest(db, nb, actor=p.email))


class AssignIn(BaseModel):
    doctor_key: str
    force: bool = False


@api.post("/{booking_id}/assign")
def assign(booking_id: str, body: AssignIn, request: Request, db: Session = Depends(get_db),
           p: Principal = Depends(require(ADMIN, OWNER))):
    b = get_booking_for(db, booking_id, p)
    try:
        res = service.assign(db, b, body.doctor_key, actor=p.email, force=body.force)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.commit()
    return {"assigned": b.assigned_doctor == body.doctor_key, "conflicts": res.conflicts,
            "meet_link": b.meet_link, "meet_action": res.meet.action, "warning": res.meet.warning,
            "booking": booking_row(db, b)}


class RescheduleIn(BaseModel):
    slot_start: datetime
    slot_end: datetime


@api.post("/{booking_id}/reschedule")
def reschedule(booking_id: str, body: RescheduleIn, db: Session = Depends(get_db),
               p: Principal = Depends(require(ADMIN, OWNER))):
    b = get_booking_for(db, booking_id, p)
    meet = service.reschedule(db, b, body.slot_start, body.slot_end, p.email)
    db.commit()
    return {"meet_action": meet.action, "warning": meet.warning, "booking": booking_row(db, b)}


@api.post("/{booking_id}/resend-intake")
def resend_intake(booking_id: str, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    b = get_booking_for(db, booking_id, p)
    url = service.send_intake_link(db, b, p.email, resend=True)
    db.commit()
    return {"sent": True, "url": url}


class LinkIn(BaseModel):
    meet_link: str


@api.post("/{booking_id}/meet-link")
def paste_meet_link(booking_id: str, body: LinkIn, db: Session = Depends(get_db),
                    p: Principal = Depends(require(ADMIN, DOCTOR, OWNER))):
    b = get_booking_for(db, booking_id, p)
    if not body.meet_link.startswith("https://"):
        raise HTTPException(422, "Paste a full https:// meeting link")
    set_manual_link(b, body.meet_link)
    audit(db, p.email, "meet.manual_link", "booking", b.id, {"link": body.meet_link})
    db.commit()
    return booking_row(db, b)


@api.post("/{booking_id}/join")
def join(booking_id: str, db: Session = Depends(get_db), p: Principal = Depends(require(DOCTOR, OWNER, ADMIN))):
    """'Join consultation': ensures the single Meet exists, opens the workspace."""
    b = get_booking_for(db, booking_id, p)
    meet = ensure_meet(db, b) if not (b.meet_link and b.meet_link_manual) else None
    from app.consult.workspace import get_or_create_consultation
    c = get_or_create_consultation(db, b, p)
    if b.status == "assigned":
        b.status = "in_consult"
    audit(db, p.email, "consult.joined", "booking", b.id, {"meet": meet.action if meet else "manual"})
    db.commit()
    return {"meet_link": b.meet_link, "warning": meet.warning if meet else
            "Using a manually pasted meeting link", "consultation_id": c.id,
            "workspace_url": f"/consult/{c.id}"}


@api.post("/{booking_id}/cancel")
def cancel(booking_id: str, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    b = get_booking_for(db, booking_id, p)
    b.status = "cancelled"
    audit(db, p.email, "booking.cancelled", "booking", b.id, commit=True)
    return booking_row(db, b)


@api.post("/{booking_id}/reviewed")
def mark_reviewed(booking_id: str, db: Session = Depends(get_db), p: Principal = Depends(require(ADMIN, OWNER))):
    b = get_booking_for(db, booking_id, p)
    b.needs_review = False
    audit(db, p.email, "booking.reviewed", "booking", b.id, commit=True)
    return booking_row(db, b)
