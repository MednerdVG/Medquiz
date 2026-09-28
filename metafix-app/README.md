# MetaFix Clinic Console & Prescription Generator

This is an internal app for Metafix Clinic, Mumbai. Bookings come in from Superprofile, the admin assigns each one to a doctor, and the patient fills an intake form through a private link. The doctor runs the consultation on a Google Meet created on their own calendar, then fills the consultation form. The app turns that form into a branded A4 prescription PDF, sends it to the patient on WhatsApp and email, and archives a copy to Google Drive.

**The doctor's input is the source of truth.** The app assembles, validates, renders and delivers. It never invents clinical content. It never calls an LLM, and it never sends anything clinical without an explicit approval click.

## Quick start (local, no external services)

```bash
pip install -r requirements.txt
sudo apt-get install fonts-lato fonts-crosextra-carlito   # or rely on the embedded fonts in assets/fonts
export DATABASE_URL=sqlite:///./metafix.db DEV_LOGIN=1 INTEGRATIONS_MODE=fake BASE_URL=http://localhost:8000
alembic upgrade head
python -m app.seed --demo          # staff accounts from doctors.yaml + a coordinator + 2 demo bookings
uvicorn app.main:app --reload
```

Open http://localhost:8000 and use **Dev sign-in**:

- `coordinator@metafix.clinic` is the admin: bookings inbox and assignment.
- `vishal@metafix.clinic` is the owner.
- `shubham@…`, `adwait@…` and `aayush@…` are doctors.

With `INTEGRATIONS_MODE=fake`, WhatsApp, email, Calendar and Drive calls are recorded instead of sent. Drive copies land in `storage/drive/Patients/…`.

To render a prescription offline (milestone 1):

```bash
python -m tools.preview examples/first_visit.json out/     # PDF + one PNG per page
```

Run the tests with `pytest`. There are 75 tests, and they cover every acceptance test in brief §21.

## Production

```bash
cp .env.example .env    # fill in the secrets
docker compose up -d --build
```

This starts four services: `app`, `worker` (RQ jobs plus the reminder loop), `db` (Postgres 16) and `redis`. Put TLS in front of the app. Keep the VM, database and bucket in an Indian region (DPDP). Set `DEV_LOGIN=0`.

## Map of the code (brief §19)

| Area | Where |
|---|---|
| §10 schema (single source of truth) | `app/rx/schema.py` |
| Layout → atomic chunks, section presets, auto-BMI, medicine anchors | `app/rx/render/layout.py` |
| Dynamic-programming packer (fewest pages first, then balanced fill) | `app/rx/render/packer.py` |
| Playwright renderer: measure → pack → compose → overflow and name check → PDF | `app/rx/render/renderer.py` |
| Letterhead / components CSS | `app/rx/render/css/rx.css` |
| Certificates | `app/rx/render/cert.py`, `app/rx/templates/certificates/` |
| Template library (diet, exercise, advice, charts), versioned YAML | `app/rx/templates/` |
| Validation engine / formulary | `app/rx/validate/rules.py`, `app/rx/validate/formulary.yaml` |
| Suggestions (suggest only) / follow-up clone and change summary | `app/rx/suggest.py`, `app/rx/followup.py` |
| Approve → render → store → deliver → archive | `app/rx/service.py` |
| Booking adapters (webhook, bridge, email, manual) and service | `app/bookings/` |
| Roles, query-layer scoping, Google sign-in, TOTP, patient tokens | `app/auth/` |
| Patient intake page, uploads, consent, portal | `app/intake/` |
| Google Meet via Calendar API / consultation workspace API | `app/consult/` |
| WhatsApp, email, Drive | `app/delivery/` |
| Reminders | `app/jobs/reminders.py` |
| UI (server-rendered Jinja + vanilla JS; mobile-usable) | `web/` |
| Doctors registry / clinic constants | `app/config/doctors.yaml`, `app/config/clinic.yaml` |

The brief allowed React or HTMX. I chose server-rendered pages with small vanilla-JS modules, so there is no build step. The consultation workspace is `web/static/consult.js`. It edits the §10 JSON directly and autosaves it.

## Key behaviours

- **Rendering is safe by construction.** A chunk is never split across pages. A section taller than a page fails with a readable error naming it. After rendering, every page is measured against the footer and checked for the patient's name; any failure is a hard error. Same input gives byte-identical PDFs (fonts are embedded and PDF dates come from the consultation date).
- **Doctors never see each other's patients.** The rule is enforced in the queries (`scope_bookings`, `scope_patients`, `get_*_for`); a direct request for another doctor's patient gets a 403.
- **A doctor approves only their own signature block.** Dual-signed prescriptions need the co-signatory to countersign. Approval locks the document. Amendments create version 2+ with a printed amendment note and a `_v2` file name. Earlier versions are never changed.
- **Warnings must each be ticked** (acknowledged) before approval. Blocking issues cannot be approved past.
- **Patient links** are HMAC-signed, expire (consultation end + 7 days for intake), are revocable, rate-limited, marked `noindex`, and disallowed in `robots.txt`.

## Needs input from the clinic before go-live

1. **Signature PNGs.** Add `assets/sig_shubham.png`, `sig_adwait_mastud.png` and `sig_aayush_gangwal.png` (transparent, max height 13 mm). Until they are there, renders for those doctors fail on purpose, and Settings → Doctors flags them. No signature image is used on certificates.
2. **Registration numbers** in `doctors.yaml` (Telemedicine Practice Guidelines). They print when present.
3. **Letterhead lines for the other doctors.** `letterhead_degrees` and `letterhead_specialty` are set only for Dr. Vishal. The others fall back to their first `sign_sub` line, and no specialty is invented.
4. **Clinical template content.** All diet, exercise, advice and chart YAML is *starter content I wrote*, and so is the formulary. Dr. Vishal should review it before it is used on real patients. Every template is versioned, and the version used is archived with each prescription.
5. **Superprofile.** Check whether the account's integrations settings expose a webhook. If they do, point it at `POST /webhooks/bookings/superprofile`, signed with `X-Signature: sha256=<HMAC of body>`. Field names are matched from a candidate list in `superprofile_webhook.py`. Otherwise use a Zapier / Pipedream / Make bridge posting the normalised JSON to the same URL, or forward confirmation emails to `POST /webhooks/bookings/email`.
6. **WhatsApp.** Business-initiated messages sent outside the 24-hour customer window must use **Meta-approved message templates**. `app/delivery/whatsapp.py` currently sends free-form text and documents. Once the templates are approved, swap in template sends for the intake link, reminders and PDF delivery.
7. **Google.** You need an OAuth client restricted to the clinic Workspace. Each doctor connects their calendar with `/auth/google/login?calendar=1`. Drive archiving needs a service account with access to the `/Patients` folder.
8. **Clinic constants** in `clinic.yaml`: phone number, address, coordinator number.

## Known limits

- Patient-name extraction for the "upload belongs to another patient" guard reads text PDFs only. Photos and scans are not OCR'd.
- The rate limiter is in-process. With more than one app instance, move it to Redis.
- The Google Calendar, Drive, WhatsApp and SMTP clients are written against the public APIs but have only been exercised in fake mode here.
