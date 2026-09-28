"""Manual entry: an admin types the booking or pastes a confirmation."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel

from app.bookings.adapters.base import BookingPatient, NormalisedBooking
from app.bookings.adapters.email_parser import EmailParserAdapter


class ManualBookingIn(BaseModel):
    external_id: Optional[str] = None
    slot_start: datetime
    slot_end: Optional[datetime] = None
    duration_minutes: int = 30
    service: Optional[str] = "Metabolic consultation — 30 min"
    amount_paid: Optional[float] = None
    currency: str = "INR"
    payment_status: str = "paid"
    patient: BookingPatient


class ManualAdapter:
    source = "manual"

    def parse(self, data: ManualBookingIn) -> NormalisedBooking:
        return NormalisedBooking(
            external_id=data.external_id or f"MAN-{uuid.uuid4().hex[:10].upper()}",
            source=self.source, booked_at=datetime.now().astimezone(),
            slot_start=data.slot_start,
            slot_end=data.slot_end or data.slot_start + timedelta(minutes=data.duration_minutes),
            service=data.service, amount_paid=data.amount_paid, currency=data.currency,
            payment_status=data.payment_status, patient=data.patient,
        )

    def parse_pasted(self, text: str) -> NormalisedBooking:
        nb = EmailParserAdapter().parse(text)
        nb.source = self.source
        return nb
