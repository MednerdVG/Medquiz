"""Best-effort patient-name extraction from text PDFs (lab reports) for the mismatch guard.

Scanned images and HEIC are not OCR'd here; they simply carry no extracted name.
"""
from __future__ import annotations

import io
import re
from typing import Optional

NAME_RE = re.compile(
    r"(?:patient(?:'s)?\s*name|name\s*of\s*(?:the\s*)?patient|patient|name)\s*[:\-]\s*"
    r"((?:mr|mrs|ms|miss|master|baby|smt|shri|dr)?\.?\s*[A-Za-z][A-Za-z .']{2,60}?)"
    r"(?=\s{2,}|\s*(?:age|sex|gender|dob|date|ref|id|uhid|lab|sample|\d)|\s*$|\n)",
    re.I | re.M,
)


def extract_patient_name(data: bytes, content_type: str) -> Optional[str]:
    if content_type != "application/pdf":
        return None
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages))))
    except Exception:
        return None
    m = NAME_RE.search(text)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return name if len(name) >= 3 else None
