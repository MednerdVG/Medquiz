"""SMTP email (fallback channel for links and PDFs)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Optional

from app.config import settings
from app.delivery.whatsapp import OUTBOX, SendResult


def send_email(to: str, subject: str, body: str, attachment: Optional[bytes] = None,
               filename: Optional[str] = None) -> SendResult:
    s = settings()
    if s.integrations_mode == "fake" or not s.smtp_host:
        OUTBOX.append({"channel": "email", "to": to, "subject": subject, "body": body,
                       "filename": filename, "size": len(attachment or b"")})
        return SendResult(True, provider_id=f"fake-email-{len(OUTBOX)}", fake=True)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    try:  # pragma: no cover - network
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return SendResult(True, provider_id=msg.get("Message-ID"))
    except Exception as exc:  # pragma: no cover - network
        return SendResult(False, error=str(exc))
