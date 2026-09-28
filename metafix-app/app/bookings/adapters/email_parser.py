"""Email ingestion fallback: parse a Superprofile booking confirmation email.

Bookings created this way are flagged `source: email_parsed` and `needs_review`.
"""
from __future__ import annotations

import email
import hashlib
import re
from datetime import datetime, timedelta
from email import policy
from zoneinfo import ZoneInfo

from app.bookings.adapters.base import BookingPatient, NormalisedBooking
from app.config import clinic

FIELD_PATTERNS = {
    "booking_id": r"(?:booking|order)\s*(?:id|#|no\.?|number)\s*[:#]?\s*([A-Z0-9-]{4,})",
    "name": r"(?:customer|name|booked by|buyer)\s*[:\-]\s*(.+)",
    "phone": r"(?:phone|mobile|whatsapp|contact)\s*(?:number|no\.?)?\s*[:\-]\s*([+\d][\d\s\-()]{8,})",
    "email": r"(?:e-?mail)\s*[:\-]\s*([\w.+-]+@[\w-]+\.[\w.-]+)",
    "service": r"(?:service|session|product|offering)\s*[:\-]\s*(.+)",
    "amount": r"(?:amount(?: paid)?|total|price)\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)",
    "date": r"(?:date|scheduled (?:on|for)|slot)\s*[:\-]\s*(.+)",
    "time": r"(?:time)\s*[:\-]\s*([\d:.]+\s*(?:am|pm)?(?:\s*(?:-|to|–)\s*[\d:.]+\s*(?:am|pm)?)?)",
    "duration": r"(\d{2,3})\s*(?:min|mins|minutes)\b",
    "notes": r"(?:notes?|message|remarks)\s*[:\-]\s*(.+)",
}
DATE_FORMATS = ["%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%Y-%m-%d", "%A, %d %B %Y", "%a, %d %b %Y"]
TIME_FORMATS = ["%I:%M %p", "%I %p", "%H:%M", "%I.%M %p"]


def _text_of(raw: str) -> str:
    head = raw.lstrip()[:2000]
    if re.search(r"^(Content-Type|MIME-Version|Received|Return-Path):", head, re.M | re.I):
        msg = email.message_from_string(raw, policy=policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
        if part and part.get_content_type() == "text/html":
            body = re.sub(r"<br\s*/?>|</p>|</tr>|</div>", "\n", body, flags=re.I)
            body = re.sub(r"<[^>]+>", " ", body)
        return f"Subject: {msg.get('subject', '')}\n{body}"
    return raw


def _parse_when(date_s: str, time_s: str | None):
    tz = ZoneInfo(clinic().get("timezone", "Asia/Kolkata"))
    date_s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_s.strip().split(" at ")[0]).strip(" .,")
    time_part = time_s
    if not time_part and " at " in date_s:
        date_s, time_part = date_s.split(" at ", 1)
    d = None
    for f in DATE_FORMATS:
        try:
            d = datetime.strptime(date_s, f)
            break
        except ValueError:
            continue
    if d is None:
        raise ValueError(f"could not read the booking date from '{date_s}'")
    start_t = end_t = None
    if time_part:
        pieces = re.split(r"\s*(?:-|to|–)\s*", time_part.strip())
        for i, piece in enumerate(pieces[:2]):
            piece = piece.upper().replace(".", ":")
            if i == 1 and not re.search(r"AM|PM", piece) and re.search(r"AM|PM", pieces[0].upper()):
                piece += " " + re.search(r"AM|PM", pieces[0].upper()).group(0)
            for f in TIME_FORMATS:
                try:
                    t = datetime.strptime(piece.strip(), f).time()
                    if i == 0:
                        start_t = t
                    else:
                        end_t = t
                    break
                except ValueError:
                    continue
    if start_t is None:
        raise ValueError("could not read the booking time")
    start = datetime.combine(d.date(), start_t, tz)
    end = datetime.combine(d.date(), end_t, tz) if end_t else None
    return start, end


class EmailParserAdapter:
    source = "email_parsed"

    def parse(self, raw_email: str) -> NormalisedBooking:
        text = _text_of(raw_email)
        found = {}
        for key, pat in FIELD_PATTERNS.items():
            m = re.search(pat, text, re.I)
            if m:
                found[key] = m.group(1).strip()
        if "date" not in found:
            raise ValueError("no booking date found in the email")
        start, end = _parse_when(found["date"], found.get("time"))
        if end is None:
            end = start + timedelta(minutes=int(found.get("duration", 30)))
        ext = found.get("booking_id") or "EM-" + hashlib.sha256(
            f"{found.get('phone')}|{found.get('email')}|{start.isoformat()}".encode()).hexdigest()[:12]
        return NormalisedBooking(
            external_id=ext if ext.startswith(("SP-", "EM-")) else f"SP-{ext}",
            source=self.source, slot_start=start, slot_end=end,
            service=found.get("service"),
            amount_paid=float(found["amount"].replace(",", "")) if found.get("amount") else None,
            payment_status="paid",
            patient=BookingPatient(name=found.get("name", "Unknown").split("\n")[0].strip(),
                                   phone=found.get("phone"), email=found.get("email"),
                                   notes_from_booking=found.get("notes")),
            raw={"email_text": text[:20000]},
        )
