"""Patient intake form (brief §7): what is collected, and how complete it is."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Medicine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    dose: str = ""
    timing: str = ""


class Vitals(BaseModel):
    model_config = ConfigDict(extra="ignore")
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    waist_cm: Optional[float] = None
    bp: Optional[str] = None
    pulse: Optional[int] = None
    fasting_sugar: Optional[str] = None
    post_meal_sugar: Optional[str] = None


class IntakeData(BaseModel):
    """Every field optional: autosave stores partial input as the patient types."""
    model_config = ConfigDict(extra="ignore")
    # identity
    name: Optional[str] = None
    age: Optional[int] = Field(None, ge=0, le=120)
    sex: Optional[Literal["M", "F", "O"]] = None
    city: Optional[str] = None
    preferred_language: Optional[str] = None
    # history
    complaints: Optional[str] = None
    duration: Optional[str] = None
    known_conditions: Optional[str] = None
    current_medicines: list[Medicine] = Field(default_factory=list)
    allergies: Optional[str] = None
    surgeries: Optional[str] = None
    family_history: Optional[str] = None
    diet_type: Optional[Literal["veg", "mixed", "vegan", "jain", "eggetarian"]] = None
    smoking: Optional[str] = None
    alcohol: Optional[str] = None
    occupation: Optional[str] = None
    sleep_hours: Optional[float] = Field(None, ge=0, le=24)
    # vitals if available
    vitals: Vitals = Field(default_factory=Vitals)


# fields that count toward the "Intake NN%" badge
CORE_FIELDS = ["name", "age", "sex", "city", "complaints", "duration", "known_conditions", "current_medicines",
               "allergies", "family_history", "diet_type", "smoking", "alcohol", "occupation", "sleep_hours"]

UPLOAD_TAGS = {"lab_report": "Lab report", "scan": "Scan", "previous_prescription": "Previous prescription",
               "ecg": "ECG", "other": "Other"}
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf",
                 "image/heic": ".heic", "image/heif": ".heif"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def merge(existing: dict, patch: dict) -> dict:
    out = dict(existing or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return IntakeData.model_validate(out).model_dump(mode="json")


def completeness(data: dict, consent: bool) -> int:
    d = data or {}
    filled = 0
    for f in CORE_FIELDS:
        v = d.get(f)
        if isinstance(v, list):
            filled += bool([m for m in v if (m or {}).get("name")])
        elif v not in (None, ""):
            filled += 1
    total = len(CORE_FIELDS) + 1
    return round(100 * (filled + (1 if consent else 0)) / total)


def sniff_type(head: bytes, declared: str) -> Optional[str]:
    """Trust magic bytes, not the browser's claim."""
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif", b"hevc"):
        return "image/heic"
    return None
