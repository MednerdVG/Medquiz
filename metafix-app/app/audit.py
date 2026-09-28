"""Audit trail: every step writes who, what, when."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(db: Session, actor: str, action: str, entity: str, entity_id: Optional[str] = None,
          detail: Optional[dict] = None, ip: Optional[str] = None, commit: bool = False) -> AuditLog:
    row = AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, detail=detail, ip=ip)
    db.add(row)
    if commit:
        db.commit()
    return row
