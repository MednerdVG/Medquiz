"""ORM models (brief §18).

patients · bookings · consultations · uploads · prescriptions · doctors (staff users) ·
availability · templates · formulary (YAML-backed; see app/rx) · messages · audit_log
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, SoftDelete, Timestamps, id_column, utcnow


class User(Base, Timestamps, SoftDelete):
    """Staff account: admin / coordinator, doctor, owner. Doctors link to the YAML registry key."""
    __tablename__ = "users"
    id: Mapped[str] = id_column()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # admin | doctor | owner
    doctor_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Google OAuth for the doctor's own calendar (encrypted at rest by the DB/volume layer)
    google_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calendar_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    in_round_robin: Mapped[bool] = mapped_column(Boolean, default=True)


class Patient(Base, Timestamps, SoftDelete):
    __tablename__ = "patients"
    id: Mapped[str] = id_column()
    patient_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # MFX-00231
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    age_years: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sex: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    preferred_language: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    bookings: Mapped[list["Booking"]] = relationship(back_populates="patient")


class Booking(Base, Timestamps, SoftDelete):
    __tablename__ = "bookings"
    id: Mapped[str] = id_column()
    external_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)  # idempotency key
    source: Mapped[str] = mapped_column(String(32))  # superprofile_webhook | zapier | email_parsed | manual
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    booked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    service: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    amount_paid: Mapped[Optional[float]] = mapped_column(nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    payment_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    notes_from_booking: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="unassigned")
    # unassigned | assigned | in_consult | awaiting_rx | completed | cancelled
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    assigned_doctor: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)  # doctor_key
    meet_link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    meet_link_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    event_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    calendar_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    patient: Mapped[Patient] = relationship(back_populates="bookings")


class IntakeForm(Base, Timestamps):
    """Patient-entered history for one booking (autosaved field by field)."""
    __tablename__ = "intake_forms"
    id: Mapped[str] = id_column()
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    consent_given_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # the exact text shown
    consent_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    consent_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AccessToken(Base, Timestamps):
    """Signed patient links (intake / portal). The signature proves origin; this row allows revocation."""
    __tablename__ = "access_tokens"
    id: Mapped[str] = id_column()  # jti
    kind: Mapped[str] = mapped_column(String(16))  # intake | portal
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Upload(Base, Timestamps, SoftDelete):
    __tablename__ = "uploads"
    id: Mapped[str] = id_column()
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    consultation_id: Mapped[Optional[str]] = mapped_column(ForeignKey("consultations.id"), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    tag: Mapped[str] = mapped_column(String(32))  # lab_report | scan | previous_prescription | ecg | other
    report_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64))  # "patient" or staff email
    extracted_patient_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detached: Mapped[bool] = mapped_column(Boolean, default=False)


class Consultation(Base, Timestamps, SoftDelete):
    __tablename__ = "consultations"
    id: Mapped[str] = id_column()
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bookings.id"), nullable=True, index=True)
    doctor_key: Mapped[str] = mapped_column(String(64), index=True)
    previous_consultation_id: Mapped[Optional[str]] = mapped_column(ForeignKey("consultations.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="draft")  # draft | awaiting_countersign | approved
    draft: Mapped[dict] = mapped_column(JSON, default=dict)  # §10 prescription JSON being edited
    change_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    follow_up_due: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # sign-offs for the current version: [{"doctor_key", "email", "at", "acknowledged"}]
    approvals: Mapped[list] = mapped_column(JSON, default=list)


class PrescriptionRecord(Base, Timestamps, SoftDelete):
    """An approved, immutable render. Corrections create a new version, never an edit."""
    __tablename__ = "prescriptions"
    __table_args__ = (UniqueConstraint("consultation_id", "version"),)
    id: Mapped[str] = id_column()
    consultation_id: Mapped[str] = mapped_column(ForeignKey("consultations.id"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    amendment_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)  # archived JSON incl. template_versions
    pdf_key: Mapped[str] = mapped_column(String(512))
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(255))
    page_count: Mapped[int] = mapped_column(Integer)
    approved_by: Mapped[str] = mapped_column(String(255))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_warnings: Mapped[list] = mapped_column(JSON, default=list)
    drive_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    delivery_status: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class Availability(Base, Timestamps, SoftDelete):
    """Weekly recurring availability window for a doctor (local clinic time)."""
    __tablename__ = "availability"
    id: Mapped[str] = id_column()
    doctor_key: Mapped[str] = mapped_column(String(64), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)


class AvailabilityException(Base, Timestamps, SoftDelete):
    """Day-level exception: a blocked period (or an extra window when available=True)."""
    __tablename__ = "availability_exceptions"
    id: Mapped[str] = id_column()
    doctor_key: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[date] = mapped_column(Date)
    start: Mapped[Optional[time]] = mapped_column(Time, nullable=True)  # None = whole day
    end: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class TemplateOverride(Base, Timestamps, SoftDelete):
    """Owner-edited template versions. YAML files ship the defaults; a row here supersedes one."""
    __tablename__ = "templates"
    id: Mapped[str] = id_column()
    kind: Mapped[str] = mapped_column(String(20))
    template_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(20))
    body: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(255))


class Message(Base, Timestamps):
    """Every outbound link / PDF / reminder. `dedupe_key` makes reminders idempotent."""
    __tablename__ = "messages"
    id: Mapped[str] = id_column()
    channel: Mapped[str] = mapped_column(String(16))  # whatsapp | email | sms
    kind: Mapped[str] = mapped_column(String(40))  # intake_link | meet_link | reminder_* | prescription | portal_link
    to: Mapped[str] = mapped_column(String(255))
    patient_id: Mapped[Optional[str]] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    attachment_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | sent | failed | fake
    provider_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)


class AuditLog(Base):
    """Append-only: who, what, when."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))  # staff email, "patient:<id>", "system", "webhook:<source>"
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Setting(Base, Timestamps):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
