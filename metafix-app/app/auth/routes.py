"""Google sign-in restricted to the clinic domain, role check, and TOTP 2FA for admin / owner."""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth import totp
from app.auth.deps import ADMIN, OWNER
from app.auth.tokens import client_ip
from app.config import settings
from app.db import get_db
from app.models import User
from app.web import render_page

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@router.get("/login")
def login_page(request: Request):
    return render_page(request, "login.html", {"dev_login": settings().dev_login})


@router.get("/google/login")
def google_login(request: Request, calendar: bool = False):
    s = settings()
    if not s.google_client_id:
        raise HTTPException(503, "Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    scope = "openid email profile" + (f" {CALENDAR_SCOPE}" if calendar else "")
    params = {"client_id": s.google_client_id, "redirect_uri": f"{s.base_url}/auth/google/callback",
              "response_type": "code", "scope": scope, "state": state, "hd": s.allowed_domain,
              "prompt": "consent" if calendar else "select_account"}
    if calendar:
        params["access_type"] = "offline"
    return RedirectResponse(f"{GOOGLE_AUTH}?{urlencode(params)}")


@router.get("/google/callback")
def google_callback(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    s = settings()
    if state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "Sign-in state mismatch — please try again")
    tok = httpx.post(GOOGLE_TOKEN, data={"code": code, "client_id": s.google_client_id,
                                         "client_secret": s.google_client_secret,
                                         "redirect_uri": f"{s.base_url}/auth/google/callback",
                                         "grant_type": "authorization_code"}, timeout=15).json()
    info = httpx.get(GOOGLE_TOKENINFO, params={"id_token": tok.get("id_token", "")}, timeout=15).json()
    if info.get("aud") != s.google_client_id or info.get("email_verified") not in ("true", True):
        raise HTTPException(403, "Google sign-in could not be verified")
    email = info.get("email", "").lower()
    if info.get("hd") != s.allowed_domain or not email.endswith("@" + s.allowed_domain):
        raise HTTPException(403, f"Only @{s.allowed_domain} accounts can sign in")
    user = db.scalar(select(User).where(User.email == email, User.deleted_at.is_(None), User.active.is_(True)))
    if not user:
        raise HTTPException(403, "Your account has not been added to the clinic console yet")
    if tok.get("refresh_token") and CALENDAR_SCOPE in tok.get("scope", ""):
        user.google_refresh_token = tok["refresh_token"]
        user.calendar_id = user.calendar_id or "primary"
        audit(db, email, "calendar.connected", "user", user.id)
    return _start_session(request, db, user)


def _start_session(request: Request, db: Session, user: User):
    request.session.clear()
    request.session["uid"] = user.id
    request.session["mfa_ok"] = False
    audit(db, user.email, "auth.sign_in", "user", user.id, ip=client_ip(request))
    db.commit()
    if user.role in (ADMIN, OWNER):
        return RedirectResponse("/auth/2fa", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.get("/2fa")
def twofa_page(request: Request, db: Session = Depends(get_db)):
    user = db.get(User, request.session.get("uid") or "")
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    enrol = None
    if not user.totp_secret:
        secret = request.session.get("totp_pending") or totp.new_secret()
        request.session["totp_pending"] = secret
        enrol = {"secret": secret, "uri": totp.provisioning_uri(secret, user.email)}
    return render_page(request, "twofa.html", {"enrol": enrol})


@router.post("/2fa")
def twofa_verify(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user = db.get(User, request.session.get("uid") or "")
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    secret = user.totp_secret or request.session.get("totp_pending")
    if not totp.verify(secret, code):
        audit(db, user.email, "auth.2fa_failed", "user", user.id, ip=client_ip(request), commit=True)
        return render_page(request, "twofa.html", {"error": "That code did not match — try again.",
                                                   "enrol": None if user.totp_secret else
                                                   {"secret": secret, "uri": totp.provisioning_uri(secret, user.email)}})
    if not user.totp_secret:
        user.totp_secret = secret
        request.session.pop("totp_pending", None)
        audit(db, user.email, "auth.2fa_enrolled", "user", user.id)
    request.session["mfa_ok"] = True
    audit(db, user.email, "auth.2fa_ok", "user", user.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


@router.post("/dev-login")
def dev_login(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    """Local development only (DEV_LOGIN=1): sign in as any seeded user, skipping Google and 2FA."""
    if not settings().dev_login:
        raise HTTPException(404)
    user = db.scalar(select(User).where(User.email == email.lower(), User.deleted_at.is_(None)))
    if not user:
        raise HTTPException(404, "No such user")
    request.session.clear()
    request.session.update({"uid": user.id, "mfa_ok": True})
    audit(db, user.email, "auth.dev_sign_in", "user", user.id, commit=True)
    return RedirectResponse("/", status_code=303)
