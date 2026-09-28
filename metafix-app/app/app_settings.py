"""Admin-switchable runtime settings stored in the `settings` table."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict[str, Any] = {
    # booking intake sources the admin has enabled (brief §5)
    "booking_sources": ["superprofile_webhook", "zapier", "email_parsed", "manual"],
    "round_robin_auto_assign": False,
    "send_intake_on": "assignment",  # assignment | booking
    "reminders_enabled": True,
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS.get(key)


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
