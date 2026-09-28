"""Database engine, session and declarative base."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class SoftDelete:
    """Soft-delete everywhere; hard delete only through an audited admin action."""
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def id_column() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


_engine = None
_SessionLocal = None


def engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = settings().database_url
        kwargs = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _):  # pragma: no cover - sqlite only
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Tests: drop the cached engine so a new DATABASE_URL takes effect."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _SessionLocal = None


def SessionLocal() -> Session:
    engine()
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    from app import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine())
