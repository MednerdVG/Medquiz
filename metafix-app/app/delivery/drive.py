"""Archive rendered PDFs to the clinic's Google Drive: /Patients/<patient_id> <name>/"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings
from app.storage import storage


@dataclass
class DriveResult:
    ok: bool
    file_id: Optional[str] = None
    path: Optional[str] = None
    error: Optional[str] = None


def folder_name(patient_code: str, patient_name: str) -> str:
    return f"{patient_code} {patient_name}".strip()


def archive_pdf(patient_code: str, patient_name: str, filename: str, pdf: bytes) -> DriveResult:
    path = f"Patients/{folder_name(patient_code, patient_name)}/{filename}"
    s = settings()
    if s.integrations_mode == "fake" or not s.drive_root_folder_id:
        # local mirror of the Drive tree inside private storage
        storage().put(f"drive/{path}", pdf, "application/pdf")
        return DriveResult(True, file_id=f"fake-drive:{path}", path=path)
    try:  # pragma: no cover - network
        token = _service_token()
        headers = {"Authorization": f"Bearer {token}"}
        patients = _ensure_folder(headers, "Patients", s.drive_root_folder_id)
        folder = _ensure_folder(headers, folder_name(patient_code, patient_name), patients)
        meta = {"name": filename, "parents": [folder], "mimeType": "application/pdf"}
        r = httpx.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
                       headers=headers, timeout=60,
                       files={"metadata": (None, json.dumps(meta), "application/json"),
                              "file": (filename, pdf, "application/pdf")})
        r.raise_for_status()
        return DriveResult(True, file_id=r.json()["id"], path=path)
    except Exception as exc:  # pragma: no cover - network
        return DriveResult(False, error=str(exc), path=path)


def _ensure_folder(headers: dict, name: str, parent: str) -> str:  # pragma: no cover - network
    q = f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and '{parent}' in parents and " \
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = httpx.get("https://www.googleapis.com/drive/v3/files", headers=headers, timeout=20,
                  params={"q": q, "fields": "files(id)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
    r.raise_for_status()
    files = r.json().get("files", [])
    if files:
        return files[0]["id"]
    r = httpx.post("https://www.googleapis.com/drive/v3/files?supportsAllDrives=true", headers=headers, timeout=20,
                   json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})
    r.raise_for_status()
    return r.json()["id"]


def _service_token() -> str:  # pragma: no cover - network
    """Service-account access token (GOOGLE_APPLICATION_CREDENTIALS) with the Drive scope."""
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.file"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token
