"""Normalise, dedupe, assign, reassign (brief §5–§6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.app_settings import get_setting
from app.audit import audit
from app.auth import tokens
from app.bookings.adapters.base import NormalisedBooking
from app.config import clinic, doctors, settings
from app.consult.meet import MeetResult, ensure_meet
from app.delivery.messaging import already_sent, notify_patient
from app.models import Availability, AvailabilityException, Booking, Patient, User


def tz() -> ZoneInfo:
    return ZoneInfo(clinic().get("timezone", "Asia/Kolkata"))


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class IngestResult:
    booking: Booking
    created: bool
    patient_created: bool
    previous_consultations: int = 0
    notes: list[str] = field(default_factory=list)


def next_patient_code(db: Session) -> str:
    n = db.scalar(select(func.count(Patient.id))) or 0
    while True:
        n += 1
        code = f"MFX-{n:05d}"
        if not db.scalar(select(Patient.id).where(Patient.patient_code == code)):
            return code


def find_or_create_patient(db: Session, nb: NormalisedBooking, actor: str) -> tuple[Patient, bool]:
    """Same phone -> same patient. Never create a duplicate silently."""
    p = None
    if nb.patient.phone:
        p = db.scalar(select(Patient).where(Patient.phone == nb.patient.phone, Patient.deleted_at.is_(None)))
    if p is None and nb.patient.email:
        p = db.scalar(select(Patient).where(func.lower(Patient.email) == nb.patient.email.lower(), Patient.deleted_at.is_(None)))
    if p:
        if nb.patient.email and not p.email:
            p.email = nb.patient.email
        return p, False
    p = Patient(patient_code=next_patient_code(db), name=nb.patient.name.strip(), phone=nb.patient.phone,
                email=nb.patient.email, age_years=nb.patient.age, sex=(nb.patient.sex or None) and nb.patient.sex[0].upper(),
                city=nb.patient.city)
    db.add(p)
    db.flush()
    audit(db, actor, "patient.created", "patient", p.id, {"patient_code": p.patient_code, "via": nb.source})
    return p, True


def ingest(db: Session, nb: NormalisedBooking, actor: str) -> IngestResult:
    """Idempotent on external_id: posting the same booking twice creates exactly one."""
    existing = db.scalar(select(Booking).where(Booking.external_id == nb.external_id))
    if existing:
        audit(db, actor, "booking.duplicate_ignored", "booking", existing.id, {"external_id": nb.external_id})
        db.commit()
        return IngestResult(existing, created=False, patient_created=False)
    patient, created_patient = find_or_create_patient(db, nb, actor)
    from app.models import Consultation
    prev = db.scalar(select(func.count(Consultation.id)).where(Consultation.patient_id == patient.id)) or 0
    b = Booking(external_id=nb.external_id, source=nb.source, needs_review=nb.source == "email_parsed",
                booked_at=nb.booked_at, slot_start=nb.slot_start, slot_end=nb.slot_end, service=nb.service,
                amount_paid=nb.amount_paid, currency=nb.currency, payment_status=nb.payment_status,
                notes_from_booking=nb.patient.notes_from_booking, raw_payload=nb.raw or None,
                patient_id=patient.id, status="unassigned")
    db.add(b)
    try:
        db.flush()
    except IntegrityError:  # a concurrent delivery of the same booking won the race
        db.rollback()
        existing = db.scalar(select(Booking).where(Booking.external_id == nb.external_id))
        return IngestResult(existing, created=False, patient_created=False)
    audit(db, actor, "booking.received", "booking", b.id,
          {"external_id": nb.external_id, "source": nb.source, "patient_code": patient.patient_code,
           "matched_existing_patient": not created_patient, "previous_consultations": prev})
    notes = []
    if not created_patient:
        notes.append(f"Linked to existing patient {patient.patient_code} ({prev} previous consultation(s))")
    if get_setting(db, "send_intake_on") == "booking":
        send_intake_link(db, b, actor)
    if get_setting(db, "round_robin_auto_assign"):
        doc = pick_round_robin(db, b)
        if doc:
            assign(db, b, doc, actor="system:round_robin")
    db.commit()
    return IngestResult(b, created=True, patient_created=created_patient, previous_consultations=prev, notes=notes)


# ---------------------------------------------------------------- availability / conflicts
def conflicts(db: Session, doctor_key: str, start: datetime, end: datetime, exclude_booking: Optional[str] = None) -> list[str]:
    out = []
    start, end = aware(start), aware(end)
    clash = db.scalars(select(Booking).where(
        Booking.assigned_doctor == doctor_key, Booking.deleted_at.is_(None), Booking.status != "cancelled",
        Booking.id != (exclude_booking or ""))).all()
    for b in clash:
        if aware(b.slot_start) < end and start < aware(b.slot_end):
            out.append(f"clashes with {b.patient.name} at {aware(b.slot_start).astimezone(tz()):%d %b %H:%M}")
    local_s, local_e = start.astimezone(tz()), end.astimezone(tz())
    for ex in db.scalars(select(AvailabilityException).where(
            AvailabilityException.doctor_key == doctor_key, AvailabilityException.day == local_s.date(),
            AvailabilityException.deleted_at.is_(None), AvailabilityException.available.is_(False))):
        if ex.start is None or (ex.start < local_e.time() and local_s.time() < (ex.end or time(23, 59))):
            out.append("falls in a blocked period" + (f" ({ex.note})" if ex.note else ""))
    windows = db.scalars(select(Availability).where(Availability.doctor_key == doctor_key,
                                                    Availability.deleted_at.is_(None))).all()
    extra = db.scalars(select(AvailabilityException).where(
        AvailabilityException.doctor_key == doctor_key, AvailabilityException.day == local_s.date(),
        AvailabilityException.available.is_(True), AvailabilityException.deleted_at.is_(None))).all()
    if windows or extra:
        spans = [(w.start, w.end) for w in windows if w.weekday == local_s.weekday()] + \
                [(x.start or time(0), x.end or time(23, 59)) for x in extra]
        if not any(s <= local_s.time() and local_e.time() <= e for s, e in spans):
            out.append("outside the doctor's marked availability")
    return out


def pick_round_robin(db: Session, b: Booking) -> Optional[str]:
    candidates = db.scalars(select(User).where(User.doctor_key.is_not(None), User.active.is_(True),
                                               User.in_round_robin.is_(True), User.deleted_at.is_(None))).all()
    free = [u for u in candidates if not conflicts(db, u.doctor_key, b.slot_start, b.slot_end, b.id)]
    if not free:
        return None
    load = {u.doctor_key: db.scalar(select(func.count(Booking.id)).where(
        Booking.assigned_doctor == u.doctor_key, Booking.slot_start >= aware(b.slot_start) - timedelta(days=7))) or 0 for u in free}
    return min(sorted(load), key=lambda k: load[k])


# ---------------------------------------------------------------- assign / reassign
@dataclass
class AssignResult:
    booking: Booking
    conflicts: list[str]
    meet: MeetResult


def assign(db: Session, b: Booking, doctor_key: str, actor: str, *, force: bool = False) -> AssignResult:
    if doctor_key not in doctors():
        raise ValueError(f"unknown doctor {doctor_key}")
    found = conflicts(db, doctor_key, b.slot_start, b.slot_end, b.id)
    if found and not force:
        return AssignResult(b, found, MeetResult(False, warning="not assigned — conflicts need confirmation"))
    previous = b.assigned_doctor
    b.assigned_doctor = doctor_key
    if b.status in ("unassigned", "assigned"):
        b.status = "assigned"
    meet = ensure_meet(db, b, previous_doctor=previous)
    audit(db, actor, "booking.reassigned" if previous else "booking.assigned", "booking", b.id,
          {"from": previous, "to": doctor_key, "conflicts_overridden": found or None, "meet": meet.action,
           "meet_warning": meet.warning})
    if previous and previous != doctor_key and b.meet_link:
        notify_patient(db, b.patient, kind="meet_link_updated", booking_id=b.id, subject="Your consultation link",
                       body=meet_message(b, updated=True))
    elif not previous and get_setting(db, "send_intake_on") == "assignment":
        send_intake_link(db, b, actor)
    return AssignResult(b, found, meet)


def reschedule(db: Session, b: Booking, start: datetime, end: datetime, actor: str) -> MeetResult:
    old = (b.slot_start, b.slot_end)
    b.slot_start, b.slot_end = start, end
    meet = ensure_meet(db, b) if b.assigned_doctor else MeetResult(True)
    audit(db, actor, "booking.rescheduled", "booking", b.id, {"from": [old[0].isoformat(), old[1].isoformat()],
                                                              "to": [start.isoformat(), end.isoformat()], "meet": meet.action})
    return meet


# ---------------------------------------------------------------- patient messages
def fmt_slot(b: Booking) -> str:
    s = aware(b.slot_start).astimezone(tz())
    return f"{s:%A} {s.day} {s:%B}, {s:%I:%M %p}".replace(" 0", " ")


def meet_message(b: Booking, updated: bool = False) -> str:
    head = "Your consultation link has changed." if updated else "Your Metafix consultation is booked."
    return (f"{head}\n{fmt_slot(b)}\nJoin on Google Meet: {b.meet_link}\n"
            "Just tap the link a minute before the time.")


def intake_url(db: Session, b: Booking) -> str:
    token = tokens.issue(db, kind="intake", patient_id=b.patient_id, booking_id=b.id,
                         expires_at=tokens.intake_expiry(b.slot_end))
    return f"{settings().base_url}/i/{token}"


def send_intake_link(db: Session, b: Booking, actor: str, *, resend: bool = False) -> Optional[str]:
    if not resend and already_sent(db, f"intake_link:{b.id}"):
        return None
    url = intake_url(db, b)
    body = (f"Hello {b.patient.name.split()[0] if b.patient.name else ''}, your Metafix consultation is on {fmt_slot(b)}.\n"
            f"Please fill in your history and upload your reports here (no login needed): {url}")
    if b.meet_link:
        body += f"\nGoogle Meet link: {b.meet_link}"
    notify_patient(db, b.patient, kind="intake_link", booking_id=b.id, subject="Your Metafix consultation — please fill in your details",
                   body=body, dedupe_key=None if resend else f"intake_link:{b.id}")
    audit(db, actor, "intake.link_sent" if not resend else "intake.link_resent", "booking", b.id)
    return url
