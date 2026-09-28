"""Shared read-only helpers over a prescription's medication lists.

Used by the layout (medicine anchors, GLP-1 supply line), the suggestion engine
and the validator. Nothing here writes clinical content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.rx.formulary import Resolved, resolve
from app.rx.schema import MedRow, Prescription

ACTIVE_LISTS = ("started", "continued", "sos", "administered", "nutrition")
DOSE_PATTERN = re.compile(r"^\s*([\d½¼¾.]+)\s*[–\-]\s*([\d½¼¾.]+)\s*[–\-]\s*([\d½¼¾.]+)")


@dataclass
class MedLine:
    list_name: str
    row: MedRow
    res: Resolved
    idx: int = 0  # position within its list

    @property
    def active(self) -> bool:
        return self.list_name in ACTIVE_LISTS


def med_lines(rx: Prescription, *, active_only: bool = False) -> list[MedLine]:
    out = []
    for list_name in ("stopped", *ACTIVE_LISTS):
        if active_only and list_name not in ACTIVE_LISTS:
            continue
        for idx, row in enumerate(getattr(rx.medications, list_name)):
            out.append(MedLine(list_name, row, resolve(row.name, row.generic), idx))
    return out


def has_molecule(rx: Prescription, *molecules: str) -> bool:
    want = {m.lower() for m in molecules}
    return any(want & set(ml.res.molecules) for ml in med_lines(rx, active_only=True))


def has_class(rx: Prescription, cls: str, lists: tuple[str, ...] = ACTIVE_LISTS) -> bool:
    return any(cls in ml.res.classes for ml in med_lines(rx) if ml.list_name in lists)


def dose_slots(dose: str | None) -> tuple[bool, bool, bool] | None:
    """'1–0–1' -> (morning, afternoon, night). None when not an m–a–n pattern."""
    m = DOSE_PATTERN.match(dose or "")
    if not m:
        return None
    return tuple(g not in ("0", "0.0") for g in m.groups())  # type: ignore[return-value]


def patient_text(rx: Prescription) -> str:
    """Everything on file about the patient's conditions, lowercased, for rule matching."""
    p = rx.patient
    parts = [*p.known_conditions, *p.allergies, *p.intolerances, *p.family_history,
             *(r.value for r in p.extra_rows), *(d.text for d in rx.diagnoses),
             *(d.detail or "" for d in rx.diagnoses), *(l.test + " " + (l.remark or "") for l in rx.labs)]
    return " ".join(parts).lower()


def is_childbearing_age_woman(rx: Prescription) -> bool:
    p = rx.patient
    if p.sex != "F" or p.age_years is None:
        return False
    if "post-menopausal" in patient_text(rx) or "postmenopausal" in patient_text(rx):
        return False
    return 15 <= p.age_years <= 50
