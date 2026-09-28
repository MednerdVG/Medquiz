"""MetaFix Clinic Console & Prescription Generator — FastAPI application."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import router as admin_router
from app.auth.routes import router as auth_router
from app.bookings.routes import api as bookings_api
from app.bookings.routes import webhooks as bookings_webhooks
from app.config import settings
from app.consult.workspace import router as consult_router
from app.intake.public import router as patient_router
from app.web import WEB_DIR, pages


def create_app() -> FastAPI:
    s = settings()
    app = FastAPI(title="MetaFix Clinic Console", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="mfx_session",
                       same_site="lax", https_only=s.base_url.startswith("https://"), max_age=12 * 3600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if s.base_url.startswith("https://"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    for r in (auth_router, bookings_webhooks, bookings_api, consult_router, admin_router, patient_router, pages):
        app.include_router(r)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return JSONResponse({"ok": True})

    return app


app = create_app()
