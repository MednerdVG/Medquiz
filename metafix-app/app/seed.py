"""Create staff accounts from the doctors registry (+ a coordinator), and optional demo bookings.

    python -m app.seed                # accounts only
    python -m app.seed --demo         # plus two demo bookings
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import doctors, settings
from app.db import SessionLocal, create_all
from app.models import User


def seed_users(db, *, coordinator_email: str | None = None) -> None:
    for key, d in doctors().items():
        if not d.email:
            continue
        u = db.scalar(select(User).where(User.email == d.email))
        if not u:
            u = User(email=d.email, name=d.sign_name, role=d.role or "doctor", doctor_key=key)
            db.add(u)
        if settings().integrations_mode == "fake" and not u.calendar_id:
            u.calendar_id = d.email  # fake calendar counts as connected in dev
    coord = coordinator_email or f"coordinator@{settings().allowed_domain}"
    if not db.scalar(select(User).where(User.email == coord)):
        db.add(User(email=coord, name="Clinic coordinator", role="admin"))
    db.commit()


def seed_demo(db) -> None:
    from app.bookings import service
    from app.bookings.adapters.base import BookingPatient, NormalisedBooking
    now = datetime.now(service.tz()).replace(minute=0, second=0, microsecond=0)
    for i, (name, phone) in enumerate([("Mrs. Afsha Khan", "+256700000001"), ("Mr. Rohan Mehta", "9820000002")]):
        start = now + timedelta(hours=2 + i * 24)
        service.ingest(db, NormalisedBooking(external_id=f"SP-DEMO-{i + 1}", source="manual", slot_start=start,
                                             slot_end=start + timedelta(minutes=30), service="Metabolic consultation — 30 min",
                                             amount_paid=1500, payment_status="paid",
                                             patient=BookingPatient(name=name, phone=phone)), actor="seed")


if __name__ == "__main__":
    create_all()
    db = SessionLocal()
    seed_users(db)
    if "--demo" in sys.argv:
        seed_demo(db)
    print("seeded:", [u.email for u in db.scalars(select(User))])
