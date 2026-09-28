"""Send-and-record: every outbound message is written to the `messages` table."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.delivery import email as email_mod
from app.delivery import whatsapp
from app.models import Message, Patient


def already_sent(db: Session, dedupe_key: str) -> bool:
    return db.scalar(select(Message.id).where(Message.dedupe_key == dedupe_key)) is not None


def notify_patient(db: Session, patient: Patient, *, kind: str, body: str, subject: str,
                   booking_id: Optional[str] = None, dedupe_key: Optional[str] = None,
                   pdf: Optional[bytes] = None, filename: Optional[str] = None,
                   attachment_key: Optional[str] = None) -> list[Message]:
    """WhatsApp first; email as well when there is an address (fallback / copy)."""
    if dedupe_key and already_sent(db, dedupe_key):
        return []
    sent: list[Message] = []
    if patient.phone:
        res = whatsapp.send_document(patient.phone, pdf, filename, body) if pdf else whatsapp.send_text(patient.phone, body)
        sent.append(Message(channel="whatsapp", kind=kind, to=patient.phone, patient_id=patient.id, booking_id=booking_id,
                            body=body, attachment_key=attachment_key, status="fake" if res.fake else ("sent" if res.ok else "failed"),
                            provider_id=res.provider_id, error=res.error,
                            dedupe_key=f"{dedupe_key}:whatsapp" if dedupe_key else None))
    wa_ok = bool(sent) and sent[0].status in ("sent", "fake")
    if patient.email and (pdf is not None or not wa_ok):
        res = email_mod.send_email(patient.email, subject, body, pdf, filename)
        sent.append(Message(channel="email", kind=kind, to=patient.email, patient_id=patient.id, booking_id=booking_id,
                            body=body, attachment_key=attachment_key, status="fake" if res.fake else ("sent" if res.ok else "failed"),
                            provider_id=res.provider_id, error=res.error,
                            dedupe_key=f"{dedupe_key}:email" if dedupe_key else None))
    if dedupe_key:
        # marker row makes the whole notification idempotent even if a channel is missing
        sent.append(Message(channel="-", kind=kind, to="-", patient_id=patient.id, booking_id=booking_id, body="",
                            status="sent", dedupe_key=dedupe_key))
    for m in sent:
        db.add(m)
    try:
        db.flush()
    except IntegrityError:  # concurrent duplicate reminder
        db.rollback()
        return []
    return [m for m in sent if m.channel != "-"]
