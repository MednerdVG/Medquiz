"""Clinic console — acceptance tests 1–4 and 12 (brief §21) plus role scoping and flows."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

ADMIN = "coordinator@metafix.clinic"
VISHAL = "vishal@metafix.clinic"
SHUBHAM = "shubham@metafix.clinic"
ADWAIT = "adwait@metafix.clinic"
SECRET = "test-webhook-secret"


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("INTEGRATIONS_MODE", "fake")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("BOOKING_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    from app import config, db, storage
    from app.auth.tokens import patient_link_limiter
    from app.consult.meet import FakeCalendar
    from app.delivery.whatsapp import OUTBOX
    config.settings.cache_clear()
    db.reset_engine()
    storage.reset_storage()
    patient_link_limiter.reset()
    FakeCalendar.EVENTS.clear()
    OUTBOX.clear()
    db.create_all()
    from app.seed import seed_users
    s = db.SessionLocal()
    seed_users(s)
    s.close()
    from app.main import create_app
    yield TestClient(create_app())
    db.reset_engine()


def as_(client, email):
    client.headers["X-Dev-User"] = email
    return client


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def slot(hours=24):
    s = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)
    return s, s + timedelta(minutes=30)


def normalised(ext="SP-88213", name="Mrs. Afsha Khan", phone="+256700000001", hours=24, email="afsha@example.com"):
    s, e = slot(hours)
    return {"external_id": ext, "source": "zapier", "booked_at": datetime.now(timezone.utc).isoformat(),
            "slot_start": s.isoformat(), "slot_end": e.isoformat(), "service": "Metabolic consultation — 30 min",
            "amount_paid": 1500, "currency": "INR", "payment_status": "paid",
            "patient": {"name": name, "phone": phone, "email": email, "age": None, "sex": None, "city": None,
                        "notes_from_booking": "Sugar high"}, "assigned_doctor": None}


def post_webhook(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/bookings/superprofile", content=body,
                       headers={"X-Signature": sign(body), "Content-Type": "application/json"})


def new_assigned_booking(client, doctor="shubham_ahirrao", **kw):
    r = post_webhook(client, normalised(**kw))
    bid = r.json()["booking_id"]
    as_(client, ADMIN)
    a = client.post(f"/api/bookings/{bid}/assign", json={"doctor_key": doctor, "force": True})
    assert a.status_code == 200, a.text
    return bid


# ---------------------------------------------------------------- acceptance 1
def test_webhook_twice_creates_one_booking(app_env):
    c = app_env
    r1 = post_webhook(c, normalised())
    r2 = post_webhook(c, normalised())
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["created"] is True and r2.json()["created"] is False
    assert r1.json()["booking_id"] == r2.json()["booking_id"]
    as_(c, ADMIN)
    assert len(c.get("/api/bookings").json()) == 1


def test_webhook_rejects_bad_signature(app_env):
    body = json.dumps(normalised()).encode()
    r = app_env.post("/webhooks/bookings/superprofile", content=body, headers={"X-Signature": "sha256=deadbeef"})
    assert r.status_code == 401


def test_native_superprofile_payload_and_patient_dedupe(app_env):
    c = app_env
    s, _ = slot()
    native = {"booking_id": "777", "start_time": s.isoformat(), "duration_minutes": 30, "product_name": "Consult",
              "amount": "1500", "customer": {"name": "Afsha Khan", "phone": "256700000001", "email": "afsha@example.com"}}
    r1 = post_webhook(c, native).json()
    r2 = post_webhook(c, normalised(ext="SP-999", phone="+256700000001")).json()
    assert r1["created"] and r2["created"]
    assert r1["patient_id"] == r2["patient_id"]  # same phone -> same patient, never a silent duplicate
    assert r2["patient_created"] is False and "Linked to existing patient" in r2["notes"][0]


def test_email_ingestion_is_flagged_for_review(app_env):
    c = app_env
    text = ("Subject: New booking\n\nBooking ID: 55012\nName: Rohan Mehta\nPhone: 9820000002\n"
            "Email: rohan@example.com\nService: Metabolic consultation — 30 min\nAmount paid: ₹1,500\n"
            "Date: 30 September 2026\nTime: 6:00 PM - 6:30 PM\n")
    body = text.encode()
    r = c.post("/webhooks/bookings/email", content=body, headers={"X-Signature": sign(body)})
    assert r.status_code == 200, r.text
    as_(c, ADMIN)
    b = c.get(f"/api/bookings/{r.json()['booking_id']}").json()
    assert b["source"] == "email_parsed" and b["needs_review"] is True
    assert b["patient"]["phone"] == "+919820000002" and b["amount_paid"] == 1500


def test_source_switched_off(app_env):
    c = as_(app_env, ADMIN)
    c.put("/api/settings", json={"booking_sources": ["manual"]})
    del c.headers["X-Dev-User"]
    assert post_webhook(c, normalised()).status_code == 409


# ---------------------------------------------------------------- acceptance 2
def test_booking_visible_only_to_assigned_doctor(app_env):
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    assert [b["id"] for b in as_(c, SHUBHAM).get("/api/bookings").json()] == [bid]
    assert as_(c, ADWAIT).get("/api/bookings").json() == []
    assert as_(c, ADWAIT).get(f"/api/bookings/{bid}").status_code == 403
    dash = as_(c, ADWAIT).get("/api/dashboard").json()
    assert all(not v for v in dash.values())
    pid = as_(c, SHUBHAM).get(f"/api/bookings/{bid}").json()["patient"]["id"]
    assert as_(c, ADWAIT).get(f"/api/patients/{pid}").status_code == 403
    assert as_(c, ADWAIT).get("/api/patients").json() == []
    assert as_(c, ADWAIT).post(f"/api/bookings/{bid}/join").status_code == 403
    assert as_(c, SHUBHAM).post(f"/api/bookings/{bid}/assign", json={"doctor_key": "adwait_mastud"}).status_code == 403


# ---------------------------------------------------------------- acceptance 3
def _intake_url(client):
    from app.delivery.whatsapp import OUTBOX
    msg = next(m for m in reversed(OUTBOX) if "/i/" in (m.get("body") or ""))
    return msg["body"].split("http://testserver")[1].split()[0]


def test_intake_link_no_login_autosave_uploads_consent_and_expiry(app_env):
    c = app_env
    bid = new_assigned_booking(c)
    url = _intake_url(c)
    anon = TestClient(c.app)
    page = anon.get(url)
    assert page.status_code == 200 and "noindex" in page.headers["x-robots-tag"]
    assert anon.patch(url + "/data", json={"complaints": "weight gain", "vitals": {"weight_kg": 92.4}}).json()["saved"]
    assert anon.get(url + "/data").json()["data"]["vitals"]["weight_kg"] == 92.4
    pdf = b"%PDF-1.4\n" + b"0" * (20 * 1024 * 1024)
    r = anon.post(url + "/uploads", files={"file": ("hba1c.pdf", pdf, "application/pdf")},
                  data={"tag": "lab_report", "report_date": "2026-09-05"})
    assert r.status_code == 200, r.text[:300]
    jpeg = b"\xff\xd8\xff\xe0" + b"1" * 5000
    r = anon.post(url + "/uploads", files={"file": ("photo.jpg", jpeg, "image/jpeg")}, data={"tag": "scan"})
    assert len(r.json()["uploads"]) == 2
    too_big = anon.post(url + "/uploads", files={"file": ("big.pdf", b"%PDF" + b"0" * (25 * 1024 * 1024 + 1), "application/pdf")})
    assert too_big.status_code == 413
    exe = anon.post(url + "/uploads", files={"file": ("x.pdf", b"MZ\x90\x00", "application/pdf")})
    assert exe.status_code == 415
    from app.config import clinic
    r = anon.post(url + "/consent", json={"agree": True, "text_shown": clinic()["consent_text"]})
    assert r.status_code == 200
    from app.db import SessionLocal
    from app.models import AccessToken, IntakeForm
    db = SessionLocal()
    form = db.query(IntakeForm).filter_by(booking_id=bid).one()
    assert form.consent_text == clinic()["consent_text"] and form.consent_given_at is not None
    badge = as_(c, SHUBHAM).get(f"/api/bookings/{bid}").json()["intake_badge"]
    assert "2 reports" in badge
    # expiry: consultation end + 7 days
    tok = db.query(AccessToken).filter_by(booking_id=bid, kind="intake").first()
    b_end = datetime.fromisoformat(as_(c, SHUBHAM).get(f"/api/bookings/{bid}").json()["slot_end"])
    assert abs((tok.expires_at.replace(tzinfo=timezone.utc) - b_end).total_seconds() - 7 * 86400) < 5
    tok.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    db.close()
    assert anon.get(url).status_code == 410


def test_tampered_intake_token_is_rejected(app_env):
    c = app_env
    new_assigned_booking(c)
    url = _intake_url(c)
    assert TestClient(c.app).get(url[:-3] + "xyz").status_code == 404


# ---------------------------------------------------------------- acceptance 4
def test_join_creates_exactly_one_meet_and_reassign_moves_it(app_env):
    from app.consult.meet import FakeCalendar
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    j1 = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()
    j2 = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()
    assert j1["meet_link"] == j2["meet_link"]
    events = [k for k in FakeCalendar.EVENTS if k[0] == SHUBHAM]
    assert len(FakeCalendar.EVENTS) == 1 and len(events) == 1
    r = as_(c, ADMIN).post(f"/api/bookings/{bid}/assign", json={"doctor_key": "adwait_mastud", "force": True}).json()
    assert r["meet_action"] == "moved"
    assert len(FakeCalendar.EVENTS) == 1
    assert list(FakeCalendar.EVENTS)[0][0] == ADWAIT
    # reschedule updates in place
    s, e = slot(48)
    as_(c, ADMIN).post(f"/api/bookings/{bid}/reschedule", json={"slot_start": s.isoformat(), "slot_end": e.isoformat()})
    assert len(FakeCalendar.EVENTS) == 1
    ev = list(FakeCalendar.EVENTS.values())[0]
    assert ev["start"].replace(tzinfo=timezone.utc) == s


def test_meet_fallback_when_calendar_not_connected(app_env):
    from app.db import SessionLocal
    from app.models import User
    db = SessionLocal()
    u = db.query(User).filter_by(email=ADWAIT).one()
    u.calendar_id = None
    db.commit()
    db.close()
    c = app_env
    r = post_webhook(c, normalised())
    res = as_(c, ADMIN).post(f"/api/bookings/{r.json()['booking_id']}/assign", json={"doctor_key": "adwait_mastud", "force": True}).json()
    assert res["meet_link"] is None and "not connected" in res["warning"]
    ok = as_(c, ADMIN).post(f"/api/bookings/{r.json()['booking_id']}/meet-link", json={"meet_link": "https://zoom.us/j/1"})
    assert ok.json()["meet_link_manual"] is True


def test_assignment_conflict_warning(app_env):
    c = app_env
    new_assigned_booking(c, doctor="shubham_ahirrao", ext="SP-1", phone="+911111111111")
    r = post_webhook(c, normalised(ext="SP-2", phone="+912222222222", name="Other"))
    res = as_(c, ADMIN).post(f"/api/bookings/{r.json()['booking_id']}/assign", json={"doctor_key": "shubham_ahirrao"}).json()
    assert res["assigned"] is False and res["conflicts"] and "clashes" in res["conflicts"][0]


# ---------------------------------------------------------------- acceptance 12 + full consultation flow
def fill_draft(c, cid, email):
    ws = as_(c, email).get(f"/api/consultations/{cid}").json()
    d = ws["draft"]
    d["patient"]["age_years"] = 55
    d["patient"]["sex"] = "F"
    d["diagnoses"] = [{"text": "Type 2 diabetes mellitus — uncontrolled"}]
    d["medications"]["started"] = [{"name": "Tab. Jardiance 10", "generic": "empagliflozin", "dose": "1–0–0", "duration": "3 months"}]
    d["advice_blocks"] = ["sglt2_precautions", "sick_day_rules"]
    d["investigations"] = [{"name": "HbA1c", "instruction": "No fasting needed"}]
    d["follow_up"] = {"interval": "3 months", "bring": [], "report_sooner_if": []}
    r = as_(c, email).put(f"/api/consultations/{cid}/draft", json=d)
    assert r.status_code == 200, r.text
    return r.json()


def test_approve_delivers_whatsapp_drive_and_audits(app_env):
    from app.delivery.whatsapp import OUTBOX
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    cid = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()["consultation_id"]
    saved = fill_draft(c, cid, SHUBHAM)
    assert not [i for i in saved["issues"] if i["level"] == "block"], saved["issues"]
    # admin cannot approve; another doctor cannot even see it
    assert as_(c, ADMIN).post(f"/api/consultations/{cid}/approve", json={}).status_code == 403
    assert as_(c, ADWAIT).get(f"/api/consultations/{cid}").status_code == 403
    warns = [i["code"] for i in saved["issues"] if i["level"] == "warn"]
    r = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={"acknowledged": warns})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "approved" and out["file_name"].startswith("Metafix_Prescription_Mrs_Afsha_Khan_")
    wa = [m for m in OUTBOX if m["channel"] == "whatsapp" and m["kind"] == "document"]
    assert len(wa) == 1 and wa[0]["to"] == "+256700000001" and wa[0]["filename"] == out["file_name"]
    assert [m for m in OUTBOX if m["channel"] == "email" and m.get("filename") == out["file_name"]]
    from app.db import SessionLocal
    from app.models import AuditLog, PrescriptionRecord
    from app.storage import storage
    db = SessionLocal()
    rec = db.get(PrescriptionRecord, out["prescription_id"])
    assert rec.drive_file_id and "Patients/MFX-00001 Mrs. Afsha Khan/" in rec.drive_file_id
    assert storage().get(f"drive/{rec.drive_file_id.split(':', 1)[1]}")[:4] == b"%PDF"
    actions = {a.action for a in db.query(AuditLog).all()}
    assert {"rx.approved", "rx.delivered", "booking.assigned", "consult.joined"} <= actions
    db.close()
    # locked after approval
    assert as_(c, SHUBHAM).put(f"/api/consultations/{cid}/draft", json=as_(c, SHUBHAM).get(f"/api/consultations/{cid}").json()["draft"]).status_code == 409
    # amendment -> version 2 with _v2 file name, v1 unchanged
    as_(c, SHUBHAM).post(f"/api/consultations/{cid}/amend", json={"note": "Dose timing corrected"})
    r2 = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={"acknowledged": warns}).json()
    assert r2["version"] == 2 and r2["file_name"].endswith("_v2.pdf")
    db = SessionLocal()
    assert db.query(PrescriptionRecord).filter_by(consultation_id=cid).count() == 2
    db.close()
    # patient portal lists both versions
    portal = next(m for m in reversed(OUTBOX) if "/p/" in (m.get("caption") or m.get("body") or ""))
    purl = (portal.get("caption") or portal["body"]).split("http://testserver")[1].split()[0]
    page = TestClient(c.app).get(purl)
    assert page.status_code == 200 and page.text.count("Download") == 2


def test_approval_blocked_and_warnings_need_ack(app_env):
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    cid = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()["consultation_id"]
    d = as_(c, SHUBHAM).get(f"/api/consultations/{cid}").json()["draft"]
    d["medications"]["started"] = [{"name": "Tab. Jardiance 10"}]  # no dose -> blocking
    as_(c, SHUBHAM).put(f"/api/consultations/{cid}/draft", json=d)
    r = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={})
    assert r.status_code == 409 and any(i["code"] == "med_dose:started:0" for i in r.json()["detail"]["issues"])
    d["medications"]["started"] = [{"name": "Tab. Vonoprazan 50 mg", "dose": "1–0–0"}]
    as_(c, SHUBHAM).put(f"/api/consultations/{cid}/draft", json=d)
    r = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={})
    assert r.status_code == 409 and any(i["code"] == "strength:started:0" for i in r.json()["detail"]["issues"])


def test_doctor_cannot_sign_as_another(app_env):
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    cid = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()["consultation_id"]
    d = fill_draft(c, cid, SHUBHAM)
    draft = as_(c, SHUBHAM).get(f"/api/consultations/{cid}").json()["draft"]
    draft["meta"]["seen_by"] = "vishal_gabale"
    as_(c, SHUBHAM).put(f"/api/consultations/{cid}/draft", json=draft)
    warns = [i["code"] for i in d["issues"] if i["level"] == "warn"]
    r = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={"acknowledged": warns})
    assert r.status_code == 403


def test_follow_up_clone_with_change_summary(app_env):
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao")
    cid = as_(c, SHUBHAM).post(f"/api/bookings/{bid}/join").json()["consultation_id"]
    saved = fill_draft(c, cid, SHUBHAM)
    warns = [i["code"] for i in saved["issues"] if i["level"] == "warn"]
    as_(c, SHUBHAM).post(f"/api/consultations/{cid}/approve", json={"acknowledged": warns})
    ws = as_(c, SHUBHAM).post(f"/api/consultations/{cid}/follow-up", json={}).json()
    new_cid = ws["consultation"]["id"]
    d = ws["draft"]
    assert d["meta"]["consult_type"] == "follow_up" and d["meta"]["visit_number"] == 2
    assert [m["name"] for m in d["medications"]["continued"]] == ["Tab. Jardiance 10"]
    d["medications"]["continued"][0]["dose"] = "1–0–1"
    d["medications"]["started"] = [{"name": "Inj. Mounjaro 2.5 mg", "dose": "once a week", "timing": "Monday"}]
    as_(c, SHUBHAM).put(f"/api/consultations/{new_cid}/draft", json=d)
    cs = as_(c, SHUBHAM).get(f"/api/consultations/{new_cid}/change-summary").json()
    assert cs["started"] == ["Inj. Mounjaro 2.5 mg"] and len(cs["dose_changed"]) == 1
    ws2 = as_(c, SHUBHAM).post(f"/api/consultations/{new_cid}/change-summary/insert").json()
    assert any(l.startswith("Started: Inj. Mounjaro") for l in ws2["draft"]["interval_review"])
    assert any(s["template"] == "glp1_weekly" for s in ws2["suggestions"])


def test_reminders_are_idempotent(app_env):
    from app.db import SessionLocal
    from app.jobs.reminders import run_due_reminders
    c = app_env
    bid = new_assigned_booking(c, doctor="shubham_ahirrao", hours=6)
    db = SessionLocal()
    first = run_due_reminders(db)
    again = run_due_reminders(db)
    assert f"reminder_intake:{bid}" in first and again == []
    from app.models import Booking
    b = db.get(Booking, bid)
    later = b.slot_start.replace(tzinfo=timezone.utc) - timedelta(minutes=20)
    assert f"reminder_meet:{bid}" in run_due_reminders(db, later)
    after = b.slot_end.replace(tzinfo=timezone.utc) + timedelta(hours=3)
    assert f"reminder_rx_pending:{bid}" in run_due_reminders(db, after)
    db.close()


def test_pages_render(app_env):
    c = app_env
    assert c.get("/auth/login").status_code == 200
    assert c.get("/", follow_redirects=False).headers["location"] == "/auth/login"
    for path in ("/bookings", "/dashboard", "/patients", "/availability", "/admin"):
        assert as_(c, ADMIN).get(path).status_code == 200, path
    assert as_(c, SHUBHAM).get("/bookings").status_code == 403
    assert c.get("/robots.txt").text.startswith("User-agent")


def test_availability_blocks_and_owner_only_audit(app_env):
    c = app_env
    s, e = slot(30)
    local_day = s.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).date().isoformat()
    as_(c, SHUBHAM).post("/api/availability/shubham_ahirrao/exceptions", json={"day": local_day, "note": "conference"})
    assert as_(c, SHUBHAM).post("/api/availability/adwait_mastud/exceptions", json={"day": local_day}).status_code == 403
    r = post_webhook(c, normalised(hours=30))
    res = as_(c, ADMIN).post(f"/api/bookings/{r.json()['booking_id']}/assign", json={"doctor_key": "shubham_ahirrao"}).json()
    assert any("blocked" in x for x in res["conflicts"])
    assert as_(c, ADMIN).get("/api/audit").status_code == 403
    assert as_(c, VISHAL).get("/api/audit").status_code == 200
