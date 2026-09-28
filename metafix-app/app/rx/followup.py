"""Follow-up visit: clone the last consultation and summarise medication changes (brief §16)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from app.rx.formulary import resolve
from app.rx.schema import MedRow, Prescription

ACTIVE = ("started", "continued", "sos", "administered", "nutrition")


@dataclass
class ChangeSummary:
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    dose_changed: list[str] = field(default_factory=list)
    continued: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.started:
            out.append("Started: " + "; ".join(self.started))
        if self.stopped:
            out.append("Stopped: " + "; ".join(self.stopped))
        if self.dose_changed:
            out.append("Dose changed: " + "; ".join(self.dose_changed))
        if self.continued:
            out.append("Continued: " + "; ".join(self.continued))
        return out

    def to_dict(self) -> dict:
        return {"started": self.started, "stopped": self.stopped, "dose_changed": self.dose_changed,
                "continued": self.continued, "interval_review_lines": self.lines()}


def _identity(row: MedRow) -> str:
    r = resolve(row.name, row.generic)
    if r.brand:
        return "brand:" + r.brand.lower()
    if r.molecules:
        return "mol:" + "+".join(sorted(r.molecules))
    return "name:" + row.name.strip().lower()


def _active(rx: Prescription) -> dict[str, MedRow]:
    out = {}
    for k in ACTIVE:
        for row in getattr(rx.medications, k):
            out.setdefault(_identity(row), row)
    return out


def _dose_sig(row: MedRow) -> tuple:
    return (row.name.strip().lower(), (row.dose or "").strip(), (row.timing or "").strip())


def change_summary(previous: Prescription, current: Prescription) -> ChangeSummary:
    prev, cur = _active(previous), _active(current)
    cs = ChangeSummary()
    for key, row in cur.items():
        if key not in prev:
            cs.started.append(row.name)
        elif _dose_sig(prev[key]) != _dose_sig(row):
            before = " ".join(x for x in [prev[key].name, prev[key].dose] if x)
            after = " ".join(x for x in [row.name, row.dose] if x)
            cs.dose_changed.append(f"{before} → {after}")
        else:
            cs.continued.append(row.name)
    for key, row in prev.items():
        if key not in cur:
            cs.stopped.append(row.name)
    return cs


def clone_for_follow_up(previous: Prescription, *, consult_date: date | None = None,
                        consultation_id: str | None = None, booking_id: str | None = None,
                        seen_by: str | None = None) -> Prescription:
    """New draft: everything carried over, visit type follow-up, previous active meds become 'continued'."""
    data = previous.model_dump(mode="json")
    meta = data["meta"]
    meta.update({
        "consultation_id": consultation_id or str(uuid.uuid4()),
        "booking_id": booking_id,
        "consult_date": (consult_date or date.today()).isoformat(),
        "consult_type": "follow_up",
        "visit_number": previous.meta.visit_number + 1,
        "previous_consultation_id": previous.meta.consultation_id,
        "seen_by": seen_by or previous.meta.seen_by,
        "status": "draft",
        "version": 1,
        "amendment_note": None,
    })
    meds = data["medications"]
    carried = meds["started"] + meds["continued"]
    for row in carried:
        row.pop("reason", None)
    meds["continued"] = carried
    meds["started"] = []
    meds["stopped"] = []
    meds["administered"] = []  # given once at the clinic; not repeated automatically
    data["certificates"] = []
    data["template_versions"] = {}
    data["interval_review"] = []
    # Measurements are never carried forward as current values: labs start empty (they can be
    # pulled forward for comparison, see previous_labs_for_comparison) and vitals keep their
    # labels with the old value shown as "earlier".
    data["labs"] = []
    data["vitals"]["weight_kg"] = None
    for k in ("waist_cm", "bp_systolic", "bp_diastolic", "pulse"):
        data["vitals"][k] = None
    data["vitals"]["strip"] = [
        {"label": c["label"], "value": "", "sub": None, "earlier": c["value"]}
        for c in data["vitals"]["strip"] if c["label"].upper() != "BMI"
    ]
    return Prescription.model_validate(data)


def previous_labs_for_comparison(previous: Prescription) -> list[dict]:
    """Lab rows for the new visit with the last values in the 'earlier' column; value left blank."""
    return [{"test": l.test, "value": "", "remark": None, "highlight": False, "earlier": l.value,
             "source": None, "date": None} for l in previous.labs]
