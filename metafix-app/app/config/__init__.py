"""Loaders for the doctors registry, clinic constants and runtime settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
APP_DIR = CONFIG_DIR.parent
ROOT_DIR = APP_DIR.parent
ASSETS_DIR = ROOT_DIR / "assets"


@dataclass(frozen=True)
class Doctor:
    key: str
    sign_name: str
    sign_sub: tuple[str, ...]
    sign_style: str = "typed"
    letterhead_name: Optional[str] = None
    letterhead_degrees: Optional[str] = None
    letterhead_specialty: Optional[str] = None
    legal_name: Optional[str] = None
    signature_png: Optional[str] = None
    registration_no: Optional[str] = None
    email: Optional[str] = None
    role: str = "doctor"

    @property
    def lh_name(self) -> str:
        return self.letterhead_name or self.sign_name

    @property
    def lh_degrees(self) -> str:
        return self.letterhead_degrees or (self.sign_sub[0] if self.sign_sub else "")

    @property
    def full_legal_name(self) -> str:
        return self.legal_name or self.sign_name

    def signature_path(self) -> Optional[Path]:
        if self.sign_style != "image" or not self.signature_png:
            return None
        override = os.environ.get("METAFIX_SIGNATURES_DIR")
        if override:
            return Path(override) / Path(self.signature_png).name
        return ROOT_DIR / self.signature_png


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache
def doctors() -> dict[str, Doctor]:
    raw = _load_yaml(Path(os.environ.get("METAFIX_DOCTORS_FILE", CONFIG_DIR / "doctors.yaml")))
    out: dict[str, Doctor] = {}
    for key, d in raw.items():
        d = dict(d)
        d["sign_sub"] = tuple(d.get("sign_sub") or ())
        out[key] = Doctor(key=key, **d)
    return out


def get_doctor(key: str) -> Doctor:
    try:
        return doctors()[key]
    except KeyError as exc:
        raise KeyError(f"Unknown doctor '{key}' — not in the doctors registry") from exc


@lru_cache
def clinic() -> dict:
    return _load_yaml(Path(os.environ.get("METAFIX_CLINIC_FILE", CONFIG_DIR / "clinic.yaml")))


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./metafix.db"))
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", "dev-insecure-change-me"))
    base_url: str = field(default_factory=lambda: os.environ.get("BASE_URL", "http://localhost:8000"))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("BOOKING_WEBHOOK_SECRET", "dev-webhook-secret"))
    storage_dir: str = field(default_factory=lambda: os.environ.get("STORAGE_DIR", "./storage"))
    storage_backend: str = field(default_factory=lambda: os.environ.get("STORAGE_BACKEND", "local"))  # local | s3
    s3_bucket: str = field(default_factory=lambda: os.environ.get("S3_BUCKET", ""))
    s3_endpoint: str = field(default_factory=lambda: os.environ.get("S3_ENDPOINT", ""))
    s3_region: str = field(default_factory=lambda: os.environ.get("S3_REGION", "ap-south-1"))
    whatsapp_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_TOKEN", ""))
    whatsapp_phone_id: str = field(default_factory=lambda: os.environ.get("WHATSAPP_PHONE_ID", ""))
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM", "care@metafix.clinic"))
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    allowed_domain: str = field(default_factory=lambda: os.environ.get("ALLOWED_DOMAIN", "metafix.clinic"))
    drive_root_folder_id: str = field(default_factory=lambda: os.environ.get("DRIVE_ROOT_FOLDER_ID", ""))
    # "fake" records outbound calls in the messages table / memory instead of hitting the network.
    integrations_mode: str = field(default_factory=lambda: os.environ.get("INTEGRATIONS_MODE", "fake"))
    dev_login: bool = field(default_factory=lambda: os.environ.get("DEV_LOGIN", "0") == "1")
    intake_grace_days: int = field(default_factory=lambda: int(os.environ.get("INTAKE_GRACE_DAYS", "7")))
    send_intake_on: str = field(default_factory=lambda: os.environ.get("SEND_INTAKE_ON", "assignment"))  # assignment | booking


@lru_cache
def settings() -> Settings:
    return Settings()
