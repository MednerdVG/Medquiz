"""Signed, expiring, revocable patient links (intake + portal). No login, never indexed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import new_id, utcnow
from app.models import AccessToken


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(body: str) -> str:
    return _b64(hmac.new(settings().secret_key.encode(), f"patient-link|{body}".encode(), hashlib.sha256).digest())


def issue(db: Session, *, kind: str, patient_id: str, booking_id: Optional[str], expires_at: datetime) -> str:
    row = AccessToken(id=new_id(), kind=kind, patient_id=patient_id, booking_id=booking_id, expires_at=expires_at)
    db.add(row)
    body = _b64(json.dumps({"j": row.id, "k": kind, "e": int(expires_at.timestamp())}, separators=(",", ":")).encode())
    return f"{body}.{_mac(body)}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve(db: Session, token: str, kind: str) -> AccessToken:
    """Return the live token row, or raise 404 (never reveal whether a token existed)."""
    try:
        body, mac = token.split(".", 1)
        if not hmac.compare_digest(_mac(body), mac):
            raise ValueError
        payload = json.loads(_unb64(body))
    except Exception:
        raise HTTPException(404, "This link is not valid.")
    if payload.get("k") != kind:
        raise HTTPException(404, "This link is not valid.")
    row = db.get(AccessToken, payload["j"])
    now = utcnow()
    if not row or row.revoked_at or _aware(row.expires_at) < now or payload["e"] < time.time():
        raise HTTPException(410, "This link has expired. Please ask the clinic for a new one.")
    row.last_used_at = now
    return row


def revoke(db: Session, token_id: str) -> None:
    row = db.get(AccessToken, token_id)
    if row and not row.revoked_at:
        row.revoked_at = utcnow()


def intake_expiry(slot_end: datetime) -> datetime:
    return _aware(slot_end) + timedelta(days=settings().intake_grace_days)


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter, per key. In-process; put Redis behind it for multi-instance deploys."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(q) >= self.limit:
                raise HTTPException(429, "Too many requests — please wait a minute and try again.")
            q.append(now)
            self.hits[key] = q

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


patient_link_limiter = RateLimiter(limit=120, window_s=60)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
