"""Booking adapter interface (brief §5).

Every source — native webhook, automation bridge, email parser, manual entry —
produces the same NormalisedBooking. Nothing downstream knows where it came from.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, Field, field_validator

Source = Literal["superprofile_webhook", "zapier", "email_parsed", "manual"]


class BookingPatient(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    city: Optional[str] = None
    notes_from_booking: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return normalise_phone(v) if v else v


class NormalisedBooking(BaseModel):
    external_id: str
    source: Source
    booked_at: Optional[datetime] = None
    slot_start: datetime
    slot_end: datetime
    service: Optional[str] = None
    amount_paid: Optional[float] = None
    currency: Optional[str] = "INR"
    payment_status: Optional[str] = None
    patient: BookingPatient
    assigned_doctor: Optional[str] = None  # set later, in the app
    raw: dict = Field(default_factory=dict, exclude=True)


class BookingAdapter(Protocol):
    source: Source

    def parse(self, payload) -> NormalisedBooking: ...


def normalise_phone(raw: str) -> str:
    """E.164-ish. Indian 10-digit numbers get +91."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return "+" + digits if digits else raw


def verify_hmac(secret: str, body: bytes, signature: Optional[str]) -> bool:
    """Accepts 'sha256=<hex>' or bare hex HMAC-SHA256 of the raw body."""
    if not signature:
        return False
    sig = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.strip().lower())
