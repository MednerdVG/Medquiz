"""Staff authentication and role checks. Role scoping is enforced in queries (see scope_*)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Booking, Consultation, Patient, User

ADMIN, DOCTOR, OWNER = "admin", "doctor", "owner"


@dataclass
class Principal:
    user: User

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def doctor_key(self) -> Optional[str]:
        return self.user.doctor_key

    @property
    def sees_all(self) -> bool:
        return self.role in (ADMIN, OWNER)

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER


def _load_user(db: Session, request: Request) -> Optional[User]:
    s = settings()
    if s.dev_login and request.headers.get("x-dev-user"):
        return db.scalar(select(User).where(User.email == request.headers["x-dev-user"], User.deleted_at.is_(None)))
    uid = request.session.get("uid") if "session" in request.scope else None
    if not uid:
        return None
    user = db.get(User, uid)
    if not user or user.deleted_at or not user.active:
        return None
    if user.role in (ADMIN, OWNER) and not request.session.get("mfa_ok"):
        return None  # second factor still pending
    return user


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    user = _load_user(db, request)
    if not user:
        raise HTTPException(401, "Sign in required")
    return Principal(user)


def optional_principal(request: Request, db: Session = Depends(get_db)) -> Optional[Principal]:
    user = _load_user(db, request)
    return Principal(user) if user else None


def require(*roles: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(403, "Not allowed for your role")
        return p
    return dep


# ---------------------------------------------------------------- query-layer scoping
def scope_bookings(stmt, p: Principal):
    stmt = stmt.where(Booking.deleted_at.is_(None))
    if p.sees_all:
        return stmt
    return stmt.where(Booking.assigned_doctor == p.doctor_key)


def scope_patients(stmt, p: Principal):
    stmt = stmt.where(Patient.deleted_at.is_(None))
    if p.sees_all:
        return stmt
    # a doctor sees a patient only through a booking or consultation assigned to them
    mine = select(Booking.patient_id).where(Booking.assigned_doctor == p.doctor_key, Booking.deleted_at.is_(None))
    mine_c = select(Consultation.patient_id).where(Consultation.doctor_key == p.doctor_key)
    return stmt.where(Patient.id.in_(mine) | Patient.id.in_(mine_c))


def get_booking_for(db: Session, booking_id: str, p: Principal) -> Booking:
    b = db.scalar(scope_bookings(select(Booking).where(Booking.id == booking_id), p))
    if not b:
        exists = db.get(Booking, booking_id)
        raise HTTPException(403 if exists and not exists.deleted_at else 404, "Not your booking")
    return b


def get_patient_for(db: Session, patient_id: str, p: Principal) -> Patient:
    pt = db.scalar(scope_patients(select(Patient).where(Patient.id == patient_id), p))
    if not pt:
        exists = db.get(Patient, patient_id)
        raise HTTPException(403 if exists and not exists.deleted_at else 404, "Not your patient")
    return pt


def get_consultation_for(db: Session, consultation_id: str, p: Principal) -> Consultation:
    stmt = select(Consultation).where(Consultation.id == consultation_id, Consultation.deleted_at.is_(None))
    if not p.sees_all:
        stmt = stmt.where(Consultation.doctor_key == p.doctor_key)
    c = db.scalar(stmt)
    if not c:
        exists = db.get(Consultation, consultation_id)
        raise HTTPException(403 if exists and not exists.deleted_at else 404, "Not your consultation")
    return c
