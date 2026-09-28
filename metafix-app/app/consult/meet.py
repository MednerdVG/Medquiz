"""Google Meet via the Calendar API on the assigned doctor's own calendar (brief §8).

Exactly one event per booking: reschedule patches it; reassignment removes it from
the old doctor's calendar and creates it on the new one, and the booking only ever
points at one event. If the doctor's calendar is not connected, fall back to a
manually pasted link and warn.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_doctor, settings
from app.models import Booking, Patient, User

API = "https://www.googleapis.com/calendar/v3"


@dataclass
class MeetResult:
    ok: bool
    meet_link: Optional[str] = None
    warning: Optional[str] = None
    action: str = "none"  # created | updated | moved | fallback | none


class CalendarClient(Protocol):
    def create(self, user: User, *, start: datetime, end: datetime, summary: str, description: str,
               attendee: Optional[str], request_id: str) -> tuple[str, str]: ...  # (event_id, meet_link)
    def update(self, user: User, event_id: str, *, start: datetime, end: datetime, summary: str) -> None: ...
    def delete(self, user: User, event_id: str) -> None: ...
    def connected(self, user: Optional[User]) -> bool: ...


class FakeCalendar:
    """In-memory calendar for dev and tests. EVENTS[(calendar_id, event_id)] = event dict."""
    EVENTS: dict[tuple[str, str], dict] = {}

    def connected(self, user):
        return bool(user and user.calendar_id)

    def create(self, user, *, start, end, summary, description, attendee, request_id):
        eid = uuid.uuid4().hex[:16]
        link = f"https://meet.google.com/fake-{eid[:3]}-{eid[3:7]}-{eid[7:10]}"
        self.EVENTS[(user.calendar_id, eid)] = {"start": start, "end": end, "summary": summary,
                                                 "attendee": attendee, "meet_link": link, "request_id": request_id}
        return eid, link

    def update(self, user, event_id, *, start, end, summary):
        ev = self.EVENTS[(user.calendar_id, event_id)]
        ev.update(start=start, end=end, summary=summary)

    def delete(self, user, event_id):
        self.EVENTS.pop((user.calendar_id, event_id), None)


class GoogleCalendar:  # pragma: no cover - network
    def connected(self, user):
        return bool(user and user.google_refresh_token)

    def _token(self, user: User) -> str:
        s = settings()
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "refresh_token": user.google_refresh_token, "grant_type": "refresh_token"})
        r.raise_for_status()
        return r.json()["access_token"]

    def _cal(self, user: User) -> str:
        return user.calendar_id or "primary"

    def create(self, user, *, start, end, summary, description, attendee, request_id):
        body = {"summary": summary, "description": description,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                "conferenceData": {"createRequest": {"requestId": request_id,
                                                     "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
                "attendees": [{"email": attendee, "optional": True}] if attendee else [],
                "guestsCanSeeOtherGuests": False, "visibility": "private"}
        r = httpx.post(f"{API}/calendars/{self._cal(user)}/events", timeout=20,
                       params={"conferenceDataVersion": 1, "sendUpdates": "all" if attendee else "none"},
                       headers={"Authorization": f"Bearer {self._token(user)}"}, json=body)
        r.raise_for_status()
        ev = r.json()
        link = ev.get("hangoutLink") or next((e["uri"] for e in ev.get("conferenceData", {}).get("entryPoints", [])
                                               if e.get("entryPointType") == "video"), None)
        return ev["id"], link

    def update(self, user, event_id, *, start, end, summary):
        r = httpx.patch(f"{API}/calendars/{self._cal(user)}/events/{event_id}", timeout=20,
                        params={"sendUpdates": "all"}, headers={"Authorization": f"Bearer {self._token(user)}"},
                        json={"summary": summary, "start": {"dateTime": start.isoformat()},
                              "end": {"dateTime": end.isoformat()}})
        r.raise_for_status()

    def delete(self, user, event_id):
        r = httpx.delete(f"{API}/calendars/{self._cal(user)}/events/{event_id}", timeout=20,
                         params={"sendUpdates": "all"}, headers={"Authorization": f"Bearer {self._token(user)}"})
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()


def calendar_client() -> CalendarClient:
    return FakeCalendar() if settings().integrations_mode == "fake" else GoogleCalendar()


def _doctor_user(db: Session, doctor_key: Optional[str]) -> Optional[User]:
    if not doctor_key:
        return None
    return db.scalar(select(User).where(User.doctor_key == doctor_key, User.deleted_at.is_(None)))


def ensure_meet(db: Session, booking: Booking, *, previous_doctor: Optional[str] = None) -> MeetResult:
    """Create, move or update the booking's single calendar event on the assigned doctor's calendar."""
    cal = calendar_client()
    doctor = _doctor_user(db, booking.assigned_doctor)
    if not booking.assigned_doctor:
        return MeetResult(False, warning="Booking is not assigned to a doctor yet")
    if not cal.connected(doctor):
        if booking.meet_link and booking.meet_link_manual:
            return MeetResult(True, booking.meet_link, action="none",
                              warning="Doctor's calendar is not connected — using the pasted meeting link")
        return MeetResult(False, warning=f"{get_doctor(booking.assigned_doctor).sign_name}'s Google Calendar is not "
                                         "connected — paste a meeting link manually", action="fallback")
    patient: Patient = booking.patient
    summary = f"Metafix consultation — {patient.name} ({patient.patient_code})"
    description = "Teleconsultation booked via the Metafix clinic console. Join from the console or the link below."

    # event already on this doctor's calendar -> update in place (reschedule)
    if booking.event_id and booking.calendar_id == doctor.calendar_id and not booking.meet_link_manual:
        cal.update(doctor, booking.event_id, start=booking.slot_start, end=booking.slot_end, summary=summary)
        return MeetResult(True, booking.meet_link, action="updated")

    action = "created"
    if booking.event_id and not booking.meet_link_manual:
        old = _doctor_user(db, previous_doctor) or db.scalar(select(User).where(User.calendar_id == booking.calendar_id))
        if old and cal.connected(old):
            cal.delete(old, booking.event_id)
        action = "moved"
    event_id, link = cal.create(doctor, start=booking.slot_start, end=booking.slot_end, summary=summary,
                                description=description, attendee=patient.email,
                                request_id=f"{booking.id}-{uuid.uuid4().hex[:8]}")
    booking.event_id, booking.calendar_id, booking.meet_link = event_id, doctor.calendar_id, link
    booking.meet_link_manual = False
    return MeetResult(True, link, action=action)


def set_manual_link(booking: Booking, link: str) -> None:
    booking.meet_link, booking.meet_link_manual = link, True
