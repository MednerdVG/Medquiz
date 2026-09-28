"""Reminders (brief §16). Idempotent: each is keyed in `messages.dedupe_key`.

- intake reminder 12 h before the slot if intake is still empty
- Meet reminder 30 min before
- prescription-pending nudge to the doctor 2 h after the slot
- follow-up reminder to the patient 3 days before the due date
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_settings import get_setting
from app.audit import audit
from app.bookings.service import aware, fmt_slot, intake_url
from app.config import settings
from app.delivery import email as email_mod
from app.delivery.messaging import already_sent, notify_patient
from app.intake.forms import completeness
from app.models import Booking, Consultation, IntakeForm, Message, Patient, Upload, User


def run_due_reminders(db: Session, now: datetime | None = None) -> list[str]:
    now = aware(now or datetime.now(timezone.utc))
    if not get_setting(db, "reminders_enabled"):
        return []
    done: list[str] = []
    active = db.scalars(select(Booking).where(Booking.deleted_at.is_(None),
                                              Booking.status.in_(("assigned", "in_consult", "awaiting_rx")))).all()
    for b in active:
        start, end = aware(b.slot_start), aware(b.slot_end)
        # 12 h before: intake still empty
        if start - timedelta(hours=12) <= now < start:
            form = db.scalar(select(IntakeForm).where(IntakeForm.booking_id == b.id))
            n_up = db.scalar(select(Upload.id).where(Upload.booking_id == b.id, Upload.deleted_at.is_(None)))
            empty = (not form or completeness(form.data, bool(form.consent_given_at)) <= 25) and not n_up
            key = f"reminder_intake:{b.id}"
            if empty and not already_sent(db, key):
                url = intake_url(db, b)
                notify_patient(db, b.patient, kind="reminder_intake", booking_id=b.id, dedupe_key=key,
                               subject="Reminder: your Metafix consultation",
                               body=f"Reminder: your consultation is on {fmt_slot(b)}. Please fill in your details "
                                    f"and upload your reports before the call: {url}")
                done.append(key)
        # 30 min before: Meet link
        if start - timedelta(minutes=30) <= now < start and b.meet_link:
            key = f"reminder_meet:{b.id}"
            if notify_patient(db, b.patient, kind="reminder_meet", booking_id=b.id, dedupe_key=key,
                              subject="Your consultation starts in 30 minutes",
                              body=f"Your Metafix consultation starts at {fmt_slot(b)}. Join here: {b.meet_link}"):
                done.append(key)
        # 2 h after: prescription pending -> nudge the doctor
        if now >= end + timedelta(hours=2) and b.assigned_doctor:
            c = db.scalar(select(Consultation).where(Consultation.booking_id == b.id, Consultation.deleted_at.is_(None)))
            if not c or c.status != "approved":
                key = f"reminder_rx_pending:{b.id}"
                doctor = db.scalar(select(User).where(User.doctor_key == b.assigned_doctor))
                if doctor and not already_sent(db, key):
                    body = (f"Prescription pending for {b.patient.name} ({b.patient.patient_code}), "
                            f"seen {fmt_slot(b)}. {settings().base_url}/")
                    res = email_mod.send_email(doctor.email, "Prescription pending", body)
                    db.add(Message(channel="email", kind="reminder_rx_pending", to=doctor.email, booking_id=b.id,
                                   body=body, status="fake" if res.fake else ("sent" if res.ok else "failed"),
                                   dedupe_key=key))
                    done.append(key)
    # follow-up: 3 days before the due date
    for c in db.scalars(select(Consultation).where(Consultation.status == "approved",
                                                   Consultation.follow_up_due.is_not(None),
                                                   Consultation.deleted_at.is_(None))):
        due = c.follow_up_due
        if (due - timedelta(days=3)) <= now.date() <= due:
            key = f"reminder_follow_up:{c.id}"
            patient = db.get(Patient, c.patient_id)
            if notify_patient(db, patient, kind="reminder_follow_up", dedupe_key=key, subject="Time for your follow-up",
                              body=f"Hello {patient.name.split()[0]}, your Metafix follow-up is due on "
                                   f"{due.day} {due:%B}. Please book your slot and bring your latest reports."):
                done.append(key)
    if done:
        audit(db, "system", "reminders.sent", "reminder", None, {"keys": done})
    db.commit()
    return done


def worker_loop(interval_s: int = 60) -> None:  # pragma: no cover - process loop
    from app.db import SessionLocal
    while True:
        db = SessionLocal()
        try:
            run_due_reminders(db)
        except Exception as exc:
            print("reminder run failed:", exc, flush=True)
        finally:
            db.close()
        _time.sleep(interval_s)


if __name__ == "__main__":  # pragma: no cover
    worker_loop()
