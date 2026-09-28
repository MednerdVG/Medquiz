"""Server-rendered pages (Jinja + small vanilla JS). The JSON API does the work."""
from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, clinic, doctors

WEB_DIR = ROOT_DIR / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.globals.update(clinic=clinic, doctors=doctors)


def render_page(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, {"request": request, **(ctx or {})}, status_code=status_code)


pages = APIRouter(include_in_schema=False)


def _principal(request: Request):
    from app.auth.deps import _load_user, Principal
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        u = _load_user(db, request)
        return Principal(u) if u else None
    finally:
        db.close()


def _show(request: Request, name: str, roles: tuple[str, ...] | None = None, **ctx):
    p = _principal(request)
    if not p:
        return RedirectResponse("/auth/login", status_code=303)
    if roles and p.role not in roles:
        return render_page(request, "error.html", {"p": p, "message": "Your role cannot open this page."}, 403)
    return render_page(request, name, {"p": p, **ctx})


def page(name: str, roles: tuple[str, ...] | None = None):
    def handler(request: Request):
        return _show(request, name, roles)
    return handler


@pages.get("/")
def home(request: Request):
    p = _principal(request)
    if not p:
        return RedirectResponse("/auth/login", status_code=303)
    return RedirectResponse("/bookings" if p.role == "admin" else "/dashboard", status_code=303)


pages.add_api_route("/bookings", page("bookings.html", ("admin", "owner")), methods=["GET"])
pages.add_api_route("/dashboard", page("dashboard.html"), methods=["GET"])
pages.add_api_route("/patients", page("patients.html"), methods=["GET"])
pages.add_api_route("/availability", page("availability.html"), methods=["GET"])
pages.add_api_route("/admin", page("admin.html", ("admin", "owner")), methods=["GET"])


@pages.get("/patients/{patient_id}")
def patient_page(request: Request, patient_id: str):
    return _show(request, "patient.html", patient_id=patient_id)


@pages.get("/consult/{consultation_id}")
def consult_page(request: Request, consultation_id: str):
    return _show(request, "consult.html", ("doctor", "owner", "admin"), consultation_id=consultation_id)
