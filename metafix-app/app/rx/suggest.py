"""Template auto-suggestion (brief §12) — suggest only, never auto-insert."""
from __future__ import annotations

from dataclasses import dataclass

from app.rx import templates as tpl
from app.rx.clinical import has_class, has_molecule, is_childbearing_age_woman, med_lines
from app.rx.schema import Prescription


@dataclass
class Suggestion:
    kind: str  # "advice"
    template: str
    reason: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "template": self.template, "reason": self.reason}


def suggest(rx: Prescription) -> list[Suggestion]:
    present = {tpl.resolve_id("advice", a) for a in rx.advice_blocks}
    out: list[Suggestion] = []

    def add(tid: str, reason: str):
        if tid not in present and all(s.template != tid for s in out):
            out.append(Suggestion("advice", tid, reason))

    if has_class(rx, "glp1"):
        weekly = [ml for ml in med_lines(rx, active_only=True) if "glp1" in ml.res.classes and ml.res.schedule == "weekly"]
        if weekly:
            add("glp1_weekly", f"{weekly[0].row.name} is a weekly GLP-1 injection")
        if is_childbearing_age_woman(rx):
            add("contraception_on_glp1", "GLP-1 prescribed to a woman of childbearing age")
    if has_class(rx, "sulfonylurea") or has_class(rx, "insulin"):
        add("hypoglycaemia", "sulfonylurea or insulin prescribed")
    if has_class(rx, "insulin"):
        add("insulin_injection_care", "insulin prescribed")
    if has_class(rx, "sglt2"):
        add("sglt2_precautions", "SGLT2 inhibitor prescribed")
        add("sick_day_rules", "SGLT2 inhibitor prescribed")
    if has_class(rx, "statin", lists=("started",)):
        add("statin_muscle_pain", "statin newly started")
    if has_molecule(rx, "levothyroxine"):
        add("thyroxine_timing", "levothyroxine prescribed")
    if any(m.res.molecules == ["ferric carboxymaltose"] for m in med_lines(rx) if m.list_name == "administered"):
        add("iron_infusion", "iron infusion given at the clinic")
    return out
