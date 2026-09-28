"""Tokenised patient pages: intake (history + uploads + consent) and the prescription portal.

No login. Signed, expiring, revocable, rate-limited links; never indexed.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth import tokens
from app.config import clinic
from app.db import get_db, new_id, utcnow
from app.intake.extract import extract_patient_name
from app.intake.forms import (ALLOWED_TYPES, MAX_UPLOAD_BYTES, UPLOAD_TAGS, IntakeData, completeness, merge,
                              sniff_type)
from app.models import Booking, IntakeForm, Patient, PrescriptionRecord, Upload
from app.storage import storage, verify_signature
from app.web import render_page

router = APIRouter(tags=["patient"])
NOINDEX = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _intake_ctx(request: Request, token: str, db: Session):
    tokens.patient_link_limiter.check(f"{token[:24]}|{tokens.client_ip(request)}")
    row = tokens.resolve(db, token, "intake")
    booking = db.get(Booking, row.booking_id)
    if not booking or booking.deleted_at:
        raise HTTPException(404, "This link is not valid.")
    form = db.scalar(select(IntakeForm).where(IntakeForm.booking_id == booking.id))
    if not form:
        p = booking.patient
        form = IntakeForm(booking_id=booking.id, data=IntakeData(name=p.name, age=p.age_years, sex=p.sex, city=p.city)
                          .model_dump(mode="json"))
        db.add(form)
        db.flush()
    return row, booking, form


def _uploads(db: Session, booking_id: str) -> list[Upload]:
    return db.scalars(select(Upload).where(Upload.booking_id == booking_id, Upload.deleted_at.is_(None))
                      .order_by(Upload.created_at)).all()


def _state(db: Session, booking: Booking, form: IntakeForm) -> dict:
    ups = _uploads(db, booking.id)
    return {"data": form.data, "consent_given_at": form.consent_given_at.isoformat() if form.consent_given_at else None,
            "percent": completeness(form.data, bool(form.consent_given_at)),
            "uploads": [{"id": u.id, "filename": u.filename, "tag": u.tag, "tag_label": UPLOAD_TAGS.get(u.tag, u.tag),
                         "report_date": u.report_date.isoformat() if u.report_date else None,
                         "size_kb": round(u.size_bytes / 1024)} for u in ups]}


@router.get("/i/{token}")
def intake_page(token: str, request: Request, db: Session = Depends(get_db)):
    row, booking, form = _intake_ctx(request, token, db)
    audit(db, f"patient:{booking.patient_id}", "intake.opened", "booking", booking.id, ip=tokens.client_ip(request))
    db.commit()
    from app.bookings.service import fmt_slot
    c = clinic()
    resp = render_page(request, "intake.html", {
        "token": token, "state": _state(db, booking, form), "slot": fmt_slot(booking), "meet_link": booking.meet_link,
        "consent_text": c["consent_text"], "purpose": c["intake_purpose"], "tags": UPLOAD_TAGS,
        "accept": ",".join(ALLOWED_TYPES) + ",.heic,.heif", "clinic": c})
    resp.headers.update(NOINDEX)
    return resp


@router.get("/i/{token}/data")
def intake_data(token: str, request: Request, db: Session = Depends(get_db)):
    _, booking, form = _intake_ctx(request, token, db)
    db.commit()
    return JSONResponse(_state(db, booking, form), headers=NOINDEX)


@router.patch("/i/{token}/data")
async def intake_autosave(token: str, request: Request, db: Session = Depends(get_db)):
    _, booking, form = _intake_ctx(request, token, db)
    try:
        patch = await request.json()
        form.data = merge(form.data, patch)
    except ValueError as exc:
        raise HTTPException(422, f"Please check that field: {exc}")
    audit(db, f"patient:{booking.patient_id}", "intake.autosaved", "booking", booking.id, {"fields": sorted(patch)})
    db.commit()
    return JSONResponse({"saved": True, "percent": completeness(form.data, bool(form.consent_given_at))}, headers=NOINDEX)


@router.post("/i/{token}/consent")
async def intake_consent(token: str, request: Request, db: Session = Depends(get_db)):
    _, booking, form = _intake_ctx(request, token, db)
    body = await request.json()
    if body.get("agree") is not True:
        raise HTTPException(422, "Consent must be ticked")
    c = clinic()
    if body.get("text_shown") and body["text_shown"].strip() != c["consent_text"].strip():
        raise HTTPException(409, "The consent text has changed — please reload the page")
    form.consent_given_at = utcnow()
    form.consent_text = c["consent_text"]
    form.consent_version = c.get("consent_version")
    form.consent_ip = tokens.client_ip(request)
    audit(db, f"patient:{booking.patient_id}", "intake.consent_given", "booking", booking.id,
          {"version": form.consent_version}, ip=form.consent_ip)
    db.commit()
    return JSONResponse({"consent_given_at": form.consent_given_at.isoformat()}, headers=NOINDEX)


@router.post("/i/{token}/uploads")
async def intake_upload(token: str, request: Request, file: UploadFile = File(...), tag: str = Form("other"),
                        report_date: Optional[str] = Form(None), db: Session = Depends(get_db)):
    _, booking, form = _intake_ctx(request, token, db)
    if tag not in UPLOAD_TAGS:
        raise HTTPException(422, "Unknown report type")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Each file can be up to 25 MB")
    if not data:
        raise HTTPException(422, "The file is empty")
    ctype = sniff_type(data[:32], file.content_type or "")
    if ctype not in ALLOWED_TYPES:
        raise HTTPException(415, "Please upload a JPEG, PNG, HEIC photo or a PDF")
    rdate = None
    if report_date:
        try:
            rdate = date.fromisoformat(report_date)
        except ValueError:
            raise HTTPException(422, "Report date should be YYYY-MM-DD")
    uid = new_id()
    key = f"uploads/{booking.patient_id}/{uid}{ALLOWED_TYPES[ctype]}"
    storage().put(key, data, ctype)
    safe_name = (file.filename or "upload").replace("/", "_").replace("\\", "_")[:200]
    up = Upload(id=uid, patient_id=booking.patient_id, booking_id=booking.id, storage_key=key, filename=safe_name,
                content_type=ctype, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), tag=tag,
                report_date=rdate, uploaded_by="patient", extracted_patient_name=extract_patient_name(data, ctype))
    db.add(up)
    audit(db, f"patient:{booking.patient_id}", "upload.added", "upload", uid,
          {"tag": tag, "bytes": len(data), "type": ctype}, ip=tokens.client_ip(request))
    db.commit()
    return JSONResponse(_state(db, booking, form), headers=NOINDEX)


@router.delete("/i/{token}/uploads/{upload_id}")
def intake_upload_delete(token: str, upload_id: str, request: Request, db: Session = Depends(get_db)):
    _, booking, form = _intake_ctx(request, token, db)
    up = db.get(Upload, upload_id)
    if not up or up.booking_id != booking.id or up.uploaded_by != "patient":
        raise HTTPException(404)
    up.deleted_at = utcnow()
    audit(db, f"patient:{booking.patient_id}", "upload.removed", "upload", up.id)
    db.commit()
    return JSONResponse(_state(db, booking, form), headers=NOINDEX)


# ---------------------------------------------------------------- patient portal
@router.get("/p/{token}")
def portal(token: str, request: Request, db: Session = Depends(get_db)):
    tokens.patient_link_limiter.check(f"{token[:24]}|{tokens.client_ip(request)}")
    row = tokens.resolve(db, token, "portal")
    patient = db.get(Patient, row.patient_id)
    rxs = db.scalars(select(PrescriptionRecord).where(PrescriptionRecord.patient_id == patient.id,
                                                      PrescriptionRecord.deleted_at.is_(None))
                     .order_by(PrescriptionRecord.approved_at.desc())).all()
    audit(db, f"patient:{patient.id}", "portal.opened", "patient", patient.id, ip=tokens.client_ip(request))
    db.commit()
    resp = render_page(request, "portal.html", {"patient": patient, "rxs": rxs, "token": token, "clinic": clinic()})
    resp.headers.update(NOINDEX)
    return resp


@router.get("/p/{token}/rx/{rx_id}")
def portal_download(token: str, rx_id: str, request: Request, db: Session = Depends(get_db)):
    tokens.patient_link_limiter.check(f"{token[:24]}|{tokens.client_ip(request)}")
    row = tokens.resolve(db, token, "portal")
    rec = db.get(PrescriptionRecord, rx_id)
    if not rec or rec.patient_id != row.patient_id or rec.deleted_at:
        raise HTTPException(404)
    audit(db, f"patient:{row.patient_id}", "portal.downloaded", "prescription", rec.id, ip=tokens.client_ip(request))
    db.commit()
    return RedirectResponse(storage().signed_url(rec.pdf_key, 120, rec.file_name), status_code=303)


@router.get("/files/{key:path}")
def signed_file(key: str, exp: int, sig: str, name: Optional[str] = None):
    """Local-storage signed URLs (S3 serves its own presigned URLs)."""
    if not verify_signature(key, exp, sig):
        raise HTTPException(403, "This download link has expired")
    data = storage().get(key)
    ctype = next((t for t, ext in ALLOWED_TYPES.items() if key.lower().endswith(ext)), "application/octet-stream")
    disp = f'inline; filename="{name}"' if name else "inline"
    return Response(data, media_type=ctype, headers={**NOINDEX, "Content-Disposition": disp})


@router.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")
