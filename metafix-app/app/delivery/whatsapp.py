"""WhatsApp Business Cloud API: text links and PDF documents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

GRAPH = "https://graph.facebook.com/v20.0"


@dataclass
class SendResult:
    ok: bool
    provider_id: Optional[str] = None
    error: Optional[str] = None
    fake: bool = False


# In fake mode every call lands here (tests and local dev read it).
OUTBOX: list[dict] = []


def _fake(kind: str, to: str, **kw) -> SendResult:
    OUTBOX.append({"channel": "whatsapp", "kind": kind, "to": to, **kw})
    return SendResult(True, provider_id=f"fake-wa-{len(OUTBOX)}", fake=True)


def send_text(to: str, body: str) -> SendResult:
    s = settings()
    if s.integrations_mode == "fake":
        return _fake("text", to, body=body)
    try:
        r = httpx.post(f"{GRAPH}/{s.whatsapp_phone_id}/messages", timeout=20,
                       headers={"Authorization": f"Bearer {s.whatsapp_token}"},
                       json={"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
                             "text": {"preview_url": True, "body": body}})
        r.raise_for_status()
        return SendResult(True, provider_id=r.json()["messages"][0]["id"])
    except Exception as exc:  # pragma: no cover - network
        return SendResult(False, error=str(exc))


def send_document(to: str, pdf: bytes, filename: str, caption: str) -> SendResult:
    s = settings()
    if s.integrations_mode == "fake":
        return _fake("document", to, filename=filename, caption=caption, size=len(pdf))
    try:  # pragma: no cover - network
        up = httpx.post(f"{GRAPH}/{s.whatsapp_phone_id}/media", timeout=60,
                        headers={"Authorization": f"Bearer {s.whatsapp_token}"},
                        data={"messaging_product": "whatsapp", "type": "application/pdf"},
                        files={"file": (filename, pdf, "application/pdf")})
        up.raise_for_status()
        media_id = up.json()["id"]
        r = httpx.post(f"{GRAPH}/{s.whatsapp_phone_id}/messages", timeout=20,
                       headers={"Authorization": f"Bearer {s.whatsapp_token}"},
                       json={"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "document",
                             "document": {"id": media_id, "filename": filename, "caption": caption}})
        r.raise_for_status()
        return SendResult(True, provider_id=r.json()["messages"][0]["id"])
    except Exception as exc:  # pragma: no cover - network
        return SendResult(False, error=str(exc))
