# Putting MetaFix online

What you end up with:

- **Doctors and the coordinator** open `https://app.metafix.clinic` (or your chosen address) and sign in with their clinic Google account.
- **Patients** never sign in. They get a private link on WhatsApp or email for the intake form and their prescriptions.

Setup is about a day of work for someone comfortable with a Linux server: a developer, or an IT freelancer with these instructions.

## 1. What you need first

| Item | Why | Where |
|---|---|---|
| A domain, e.g. `metafix.clinic` | the web address | any registrar (GoDaddy, Namecheap…) |
| **Google Workspace** on that domain | staff sign-in is limited to `@yourdomain` accounts, and it provides Calendar/Meet and Drive | workspace.google.com |
| A cloud server **in Mumbai** | patient data must stay in India (DPDP) | AWS Lightsail / EC2 `ap-south-1`, Google Cloud `asia-south1`, DigitalOcean BLR, Azure Central India |
| WhatsApp Business Cloud API | sending links and PDFs | Meta Business Manager → WhatsApp |
| An email account for sending (SMTP) | email fallback | Google Workspace SMTP relay, Zoho, SES |

Server size: **2 vCPU, 4 GB RAM, 40 GB disk**, Ubuntu 24.04. The PDF renderer runs Chrome, so don't go below 4 GB.

If the doctors use personal `@gmail.com` accounts instead of a Workspace domain, tell the developer first. Sign-in is built to accept a single clinic domain only.

## 2. Server setup (one time)

```bash
# on the server
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
git clone -b claude/metafix-clinic-console-rx-56umoa https://github.com/MednerdVG/Medquiz.git
cd Medquiz/metafix-app
cp .env.example .env
nano .env        # fill in every value — see step 4
```

In the server's firewall, open ports **80** and **443** only, plus 22 for SSH.

## 3. Point the address at the server

At your domain registrar, add a DNS **A record**: `app` → the server's public IP. Wait until `ping app.metafix.clinic` shows that IP.

## 4. Fill in `.env`

| Setting | Value |
|---|---|
| `DOMAIN` | `app.metafix.clinic` |
| `BASE_URL` | `https://app.metafix.clinic` |
| `ALLOWED_DOMAIN` | `metafix.clinic` |
| `SECRET_KEY`, `POSTGRES_PASSWORD`, `BOOKING_WEBHOOK_SECRET` | long random strings (`openssl rand -hex 32`) |
| `INTEGRATIONS_MODE` | `live` (use `fake` for a dry run that sends nothing) |
| `DEV_LOGIN` | **`0`**, always, on a public server |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | step 5 |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` | from Meta's WhatsApp setup page |
| `SMTP_*` | your sending mailbox |
| `DRIVE_ROOT_FOLDER_ID` | ID of the Drive folder that will hold `/Patients` |

## 5. Google setup

1. Go to https://console.cloud.google.com, create a project and enable the **Google Calendar API** and the **Google Drive API**.
2. **OAuth consent screen**: choose type **Internal** (Workspace users only).
3. **Credentials → Create OAuth client ID → Web application**:
   - Authorised redirect URI: `https://app.metafix.clinic/auth/google/callback`
   - Copy the client ID and secret into `.env`.
4. **Drive archive**: create a **service account**, download its JSON key to `metafix-app/secrets/drive-service-account.json`, and share the Drive archive folder with the service account's email address.

## 6. Doctors' details

Before starting, edit these files on the server:

- `app/config/doctors.yaml`:
  - Each doctor's `email` must be their real Workspace address.
  - Add each `registration_no`.
  - Set `role: owner` for Dr. Vishal.
- `app/config/clinic.yaml`: clinic phone, address and coordinator number.
- Signature scans: put `sig_shubham.png`, `sig_adwait_mastud.png` and `sig_aayush_gangwal.png` (transparent PNG) in `assets/`.

## 7. Start it

```bash
docker compose up -d --build
docker compose logs -f app      # wait for "Uvicorn running"
```

Open `https://app.metafix.clinic`. The HTTPS certificate is issued automatically on first visit, which can take up to a minute.

**First sign-in:**

1. Each doctor clicks **Sign in with Google**.
2. Admin and owner accounts are then asked to set up an authenticator app (Google Authenticator) for 2FA.
3. Each doctor connects their calendar once by visiting `https://app.metafix.clinic/auth/google/login?calendar=1`. Meet links are created on their calendar from then on.
4. Owner → **Settings**: check the Doctors table shows every calendar connected and no "signature PNG missing".

**Coordinator account:** add the coordinator's real email under Settings → Staff accounts, role `admin`.

## 8. Connect Superprofile bookings

Pick one route under Settings → Booking intake sources:

- **Webhook**, if Superprofile's integrations page offers one.
  - URL: `https://app.metafix.clinic/webhooks/bookings/superprofile`
  - Each request must carry `X-Signature: sha256=<HMAC-SHA256 of the body with BOOKING_WEBHOOK_SECRET>`.
- **Zapier / Pipedream / Make**: trigger on "new Superprofile booking", then a "POST JSON" step to the same URL. Build the body in the format shown in the brief §5 and sign it the same way.
- **Email**: forward the booking-confirmation emails to an inbound-mail service (Mailgun / SendGrid inbound parse) that posts them to `/webhooks/bookings/email`.
- **Manual**: always available. The coordinator uses **+ Manual booking** or **Paste confirmation**.

## 9. WhatsApp message templates

Meta only allows free-form messages within 24 hours of the patient last messaging you. For the intake link, reminders and the prescription PDF, create and get approval for **message templates** in Meta Business Manager. The developer then switches `app/delivery/whatsapp.py` to send those templates. Until that's done, use email as the main channel or ask patients to message the clinic first.

## 10. Before real patients

- [ ] Dr. Vishal reviews all diet, exercise, advice and chart templates and the formulary. They are starter content.
- [ ] Do a full dry run with `INTEGRATIONS_MODE=fake`: book → assign → intake → join → approve. Check the PDF.
- [ ] Switch to `live` and run one real booking on a staff member's own phone.
- [ ] Turn on automatic daily server snapshots (backups) in your cloud provider.

## Day-to-day

| Task | Command |
|---|---|
| Update to the latest code | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f app worker` |
| Database backup | `docker compose exec db pg_dump -U metafix metafix > backup.sql` |
