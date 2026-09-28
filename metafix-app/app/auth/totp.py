"""RFC 6238 TOTP for admin / owner second factor (no external dependency)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _code(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(val).zfill(digits)


def now_code(secret: str, at: float | None = None) -> str:
    return _code(secret, int((at or time.time()) // 30))


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    if not secret or not code or not code.strip().isdigit():
        return False
    t = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_code(secret, t + w), code.strip()) for w in range(-window, window + 1))


def provisioning_uri(secret: str, email: str, issuer: str = "Metafix Clinic") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}"
