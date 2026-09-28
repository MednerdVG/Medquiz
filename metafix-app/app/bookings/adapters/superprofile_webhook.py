"""Native Superprofile webhook / API adapter.

Superprofile does not publish a documented booking API, so field names are
looked up from a list of candidates; add names here if the account's payload
differs. Anything unrecognised is kept in `raw` for the admin to review.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.bookings.adapters.base import BookingPatient, NormalisedBooking

CANDIDATES = {
    "external_id": ["booking_id", "id", "order_id", "bookingId", "orderId"],
    "booked_at": ["created_at", "booked_at", "createdAt", "purchase_date"],
    "slot_start": ["slot_start", "start_time", "startTime", "scheduled_at", "session_start", "appointment_time"],
    "slot_end": ["slot_end", "end_time", "endTime", "session_end"],
    "duration": ["duration_minutes", "duration", "slot_duration"],
    "service": ["service", "product_name", "title", "offering", "session_title"],
    "amount": ["amount_paid", "amount", "price", "total"],
    "currency": ["currency"],
    "payment_status": ["payment_status", "status", "paymentStatus"],
    "name": ["name", "customer_name", "full_name", "buyer_name"],
    "phone": ["phone", "mobile", "phone_number", "whatsapp", "customer_phone"],
    "email": ["email", "customer_email", "buyer_email"],
    "notes": ["notes", "message", "answers", "questions", "remarks"],
}


def _find(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    for nested in ("data", "booking", "customer", "buyer", "user", "order"):
        if isinstance(d.get(nested), dict):
            v = _find(d[nested], keys)
            if v not in (None, ""):
                return v
    return None


def _dt(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e12 else 1)).astimezone()
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class SuperprofileWebhookAdapter:
    source = "superprofile_webhook"

    def parse(self, payload: dict) -> NormalisedBooking:
        start = _dt(_find(payload, CANDIDATES["slot_start"]))
        if start is None:
            raise ValueError("booking payload has no slot start time")
        end = _dt(_find(payload, CANDIDATES["slot_end"]))
        if end is None:
            end = start + timedelta(minutes=int(_find(payload, CANDIDATES["duration"]) or 30))
        notes = _find(payload, CANDIDATES["notes"])
        if isinstance(notes, (list, dict)):
            notes = str(notes)
        ext = _find(payload, CANDIDATES["external_id"])
        if not ext:
            raise ValueError("booking payload has no booking id")
        return NormalisedBooking(
            external_id=f"SP-{ext}" if not str(ext).startswith("SP-") else str(ext),
            source=self.source,
            booked_at=_dt(_find(payload, CANDIDATES["booked_at"])),
            slot_start=start, slot_end=end,
            service=_find(payload, CANDIDATES["service"]),
            amount_paid=float(_find(payload, CANDIDATES["amount"]) or 0) or None,
            currency=_find(payload, CANDIDATES["currency"]) or "INR",
            payment_status=str(_find(payload, CANDIDATES["payment_status"]) or "paid").lower(),
            patient=BookingPatient(name=_find(payload, CANDIDATES["name"]) or "Unknown",
                                   phone=_find(payload, CANDIDATES["phone"]),
                                   email=_find(payload, CANDIDATES["email"]),
                                   notes_from_booking=notes),
            raw=payload,
        )
