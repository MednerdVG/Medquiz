"""Validation engine (brief §15).

`validate(rx, ...)` returns a list of Issues. BLOCK issues prevent approval;
WARN issues must each be acknowledged (by code) before approval; INFO is shown only.
Render-time checks (page overflow, patient name on every page) are added by
`render_issues()` from a RenderError.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

from app.config import doctors
from app.rx import templates as tpl
from app.rx.bmi import compute_bmi
from app.rx.clinical import ACTIVE_LISTS, med_lines, patient_text
from app.rx.formulary import STRENGTH_RE, strength_key, strength_ok
from app.rx.schema import Prescription

BLOCK, WARN, INFO = "block", "warn", "info"


@dataclass
class Issue:
    level: str
    code: str  # stable id used for acknowledgement, e.g. "strength:started:0"
    message: str
    field: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_name(s: str) -> str:
    s = re.sub(r"^(mr|mrs|ms|miss|dr|master|baby|smt|shri)\.?\s+", "", (s or "").strip().lower())
    return re.sub(r"[^a-z ]", "", s).strip()


def names_match(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb or set(na.split()) <= set(nb.split()) or set(nb.split()) <= set(na.split()):
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.85


# ---------------------------------------------------------------- schedule detection
DAILY_RE = re.compile(r"\b(daily|every day|once a day|twice a day|od|bd|tds|hs|at night|morning|evening|bedtime)\b", re.I)
WEEKLY_RE = re.compile(r"\b(week|weekly|once a week|every (mon|tues|wednes|thurs|fri|satur|sun)day)\b", re.I)
SIX_MONTHLY_RE = re.compile(r"\b(6|six)[- ]?month|\b(3|three)[- ]?month|\bday 90\b|\bmonth(s)? 3\b", re.I)
DOSE_TRIPLET = re.compile(r"^\s*[\d½¼¾.]+\s*[–\-]\s*[\d½¼¾.]+\s*[–\-]\s*[\d½¼¾.]+")


def written_schedule(row) -> Optional[str]:
    text = " ".join(x for x in [row.dose, row.timing, row.duration, row.note] if x)
    if WEEKLY_RE.search(text):
        return "weekly"
    if SIX_MONTHLY_RE.search(text) and "daily" not in text.lower():
        return "six_monthly"
    if DOSE_TRIPLET.match(row.dose or "") or DAILY_RE.search(text):
        return "daily"
    return None


# ---------------------------------------------------------------- rules
def validate(rx: Prescription, *, stored_patient: dict | None = None, uploads: Iterable[dict] = ()) -> list[Issue]:
    issues: list[Issue] = []
    add = issues.append
    p, m = rx.patient, rx.meta

    # ---- blocking: identity
    if not p.name.strip():
        add(Issue(BLOCK, "missing:patient_name", "Patient name is missing.", "patient.name"))
    if not m.consult_date:
        add(Issue(BLOCK, "missing:consult_date", "Consultation date is missing.", "meta.consult_date"))
    if not m.seen_by:
        add(Issue(BLOCK, "missing:seen_by", "'Seen by' is required — choose the treating doctor.", "meta.seen_by"))
    elif m.seen_by not in doctors():
        add(Issue(BLOCK, "unknown:seen_by", f"'{m.seen_by}' is not in the doctors registry.", "meta.seen_by"))
    if m.co_signatory and m.co_signatory not in doctors():
        add(Issue(BLOCK, "unknown:co_signatory", f"'{m.co_signatory}' is not in the doctors registry.", "meta.co_signatory"))

    # ---- blocking: stored record
    if stored_patient:
        for fld, label in (("name", "name"), ("age_years", "age"), ("sex", "sex"), ("patient_id", "patient ID")):
            stored = stored_patient.get(fld)
            given = getattr(p, fld)
            if stored in (None, "") or given in (None, ""):
                continue
            if fld == "name":
                differs = not names_match(stored, given)
            elif fld == "age_years":
                differs = abs(int(stored) - int(given)) > 1  # birthday between visits
            else:
                differs = str(stored).strip().upper() != str(given).strip().upper()
            if differs:
                add(Issue(BLOCK, f"record_mismatch:{fld}",
                          f"Patient {label} '{given}' differs from the stored record '{stored}' for {p.patient_id or 'this patient'}.",
                          f"patient.{fld}"))

    # ---- blocking: uploads for another patient
    for u in uploads:
        extracted = u.get("extracted_patient_name")
        if extracted and p.name and not names_match(extracted, p.name):
            add(Issue(BLOCK, f"upload_mismatch:{u.get('id')}",
                      f"Upload '{u.get('filename', u.get('id'))}' appears to belong to '{extracted}', not {p.name}. "
                      "Detach it before approving.", "uploads"))

    # ---- blocking: medication rows
    lines = med_lines(rx)
    for ml in lines:
        ln, row, idx = ml.list_name, ml.row, ml.idx
        fld = f"medications.{ln}[{idx}]"
        if not row.name.strip():
            add(Issue(BLOCK, f"med_name:{ln}:{idx}", "A medication row has no medicine name.", fld))
        if ln in ("started", "continued", "nutrition") and not (row.dose or "").strip():
            add(Issue(BLOCK, f"med_dose:{ln}:{idx}", f"{row.name or 'A medicine'} has no dose / frequency.", fld))
        if ln == "administered" and not ((row.dose or "").strip() or (row.route or "").strip()):
            add(Issue(BLOCK, f"med_dose:{ln}:{idx}", f"{row.name} (given at the clinic) has no dose or route.", fld))

    def keyset(ml):
        return {ml.res.brand.lower()} if ml.res.brand else set()

    by_list: dict[str, list] = {}
    for ml in lines:
        by_list.setdefault(ml.list_name, []).append(ml)
    for a in by_list.get("started", []):
        for b in by_list.get("continued", []):
            same = (keyset(a) & keyset(b)) or (set(a.res.molecules) & set(b.res.molecules)) or \
                _norm_name(a.row.name) == _norm_name(b.row.name)
            if same:
                add(Issue(BLOCK, f"dup_started_continued:{_slug(a.row.name)}",
                          f"{a.row.name} and {b.row.name} are both 'started' and 'continued' (same drug or molecule).",
                          "medications"))
    for s in by_list.get("stopped", []):
        for ln in ACTIVE_LISTS:
            for b in by_list.get(ln, []):
                same = (keyset(s) & keyset(b)) or _norm_name(s.row.name) == _norm_name(b.row.name) or \
                    (s.res.molecules and set(s.res.molecules) == set(b.res.molecules))
                if same:
                    add(Issue(BLOCK, f"dup_stopped_active:{_slug(s.row.name)}:{ln}",
                              f"{s.row.name} is stopped but {b.row.name} is still in '{ln}'.", "medications"))

    for i, lab in enumerate(rx.labs):
        if not lab.test.strip() or not lab.value.strip():
            add(Issue(BLOCK, f"lab_incomplete:{i}", f"Lab row {i + 1} ({lab.test or 'unnamed'}) has no value.", f"labs[{i}]"))

    # ---- warnings: formulary strength
    for ml in lines:
        if ml.list_name == "stopped":
            continue
        ok = strength_ok(ml.res)
        idx = ml.idx
        if ok is False:
            known = ", ".join(ml.res.known_strengths)
            what = ml.res.brand or ml.res.generic
            add(Issue(WARN, f"strength:{ml.list_name}:{idx}",
                      f"{ml.row.name}: strength {ml.res.strength} is not a marketed strength of {what} (formulary: {known}).",
                      f"medications.{ml.list_name}[{idx}]"))
        elif not ml.res.in_formulary and ml.list_name != "nutrition":
            add(Issue(INFO, f"not_in_formulary:{ml.list_name}:{idx}", f"{ml.row.name} is not in the formulary — check the spelling and strength.",
                      f"medications.{ml.list_name}[{idx}]"))

    # ---- warnings: schedule vs drug
    for ml in lines:
        if not ml.active:
            continue
        expected = ml.res.schedule
        written = written_schedule(ml.row)
        idx = ml.idx
        if expected in ("weekly", "six_monthly") and written == "daily":
            add(Issue(WARN, f"schedule:{ml.list_name}:{idx}",
                      f"{ml.row.name} is a {expected.replace('_', '-')} medicine but is written as daily.",
                      f"medications.{ml.list_name}[{idx}]"))
        elif expected == "six_monthly" and written == "weekly":
            add(Issue(WARN, f"schedule:{ml.list_name}:{idx}",
                      f"{ml.row.name} is given every 6 months (after the 3-month dose), not weekly.",
                      f"medications.{ml.list_name}[{idx}]"))
        elif expected == "daily" and written == "weekly" and "glp1" in ml.res.classes:
            add(Issue(WARN, f"schedule:{ml.list_name}:{idx}",
                      f"{ml.row.name} is a daily GLP-1 but is written as weekly.", f"medications.{ml.list_name}[{idx}]"))

    # ---- warnings: implausible measurements
    issues.extend(_measurement_issues(rx))

    # ---- warnings: duplication and interaction
    active = [ml for ml in lines if ml.active]
    seen: dict[str, str] = {}
    for ml in active:
        for mol in ml.res.molecules:
            if mol in seen and seen[mol] != ml.row.name:
                add(Issue(WARN, f"dup_molecule:{_slug(mol)}", f"{seen[mol]} and {ml.row.name} both contain {mol}.", "medications"))
            seen.setdefault(mol, ml.row.name)
    classes = set().union(*(ml.res.classes for ml in active)) if active else set()
    advice = {tpl.resolve_id("advice", a) for a in rx.advice_blocks}
    if ({"sulfonylurea", "insulin"} & classes) and "glp1" in classes and "hypoglycaemia" not in advice:
        add(Issue(WARN, "interaction:su_insulin_glp1", "Sulfonylurea or insulin with a GLP-1 and no hypoglycaemia advice block.", "advice_blocks"))
    if "sglt2" in classes and "sick_day_rules" not in advice:
        add(Issue(WARN, "interaction:sglt2_sickday", "SGLT2 inhibitor prescribed with no sick-day rules block.", "advice_blocks"))
    ptxt = patient_text(rx)
    started_classes = set().union(*(ml.res.classes for ml in by_list.get("started", []))) if by_list.get("started") else set()
    intolerance_text = " ".join(p.intolerances + p.allergies + p.known_conditions).lower()
    if "statin" in started_classes and ("statin" in intolerance_text or re.search(r"statin[- ](intoleran|myopathy|induced)", ptxt)):
        add(Issue(WARN, "interaction:statin_intolerance", "A statin is started but a statin intolerance is recorded.", "medications.started"))
    if "nsaid" in classes and re.search(r"\b(ckd|chronic kidney|albuminuria|microalbuminuria|proteinuria|nephropathy|egfr\s*(<|under)\s*60)\b", ptxt):
        add(Issue(WARN, "interaction:nsaid_kidney", "NSAID prescribed with albuminuria / CKD on file.", "medications"))

    # ---- warnings: age-related
    for ml in active:
        if "zolpidem" in ml.res.molecules:
            dose = strength_key(ml.res.strength or _first_strength(ml.row.name))
            if dose and dose[0] > 5 and (p.sex == "F" or (p.age_years or 0) > 65):
                add(Issue(WARN, f"age:zolpidem:{_slug(ml.row.name)}",
                          f"{ml.row.name}: zolpidem above 5 mg in a woman or anyone over 65.", "medications"))
        if "benzodiazepine" in ml.res.classes and (p.age_years or 0) > 65:
            add(Issue(WARN, f"age:benzo:{_slug(ml.row.name)}", f"{ml.row.name}: benzodiazepine in a patient over 65.", "medications"))

    # ---- warnings: follow-up / investigations
    has_fu = bool(rx.follow_up and rx.follow_up.interval)
    if has_fu and not rx.investigations:
        add(Issue(WARN, "followup_no_investigations", "Follow-up is stated but no investigations are advised.", "investigations"))
    if rx.investigations and not has_fu:
        add(Issue(WARN, "investigations_no_followup", "Investigations are advised but no follow-up interval is given.", "follow_up"))

    # ---- warnings: referral named in text but not listed
    listed = " ".join(f"{r.to} {r.named_doctor or ''}" for r in rx.referrals).lower()
    for spec in _referrals_in_text(rx):
        if spec.lower() not in listed:
            add(Issue(WARN, f"referral_missing:{_slug(spec)}",
                      f"The text mentions a referral to {spec}, but it is not in the referral list.", "referrals"))

    # ---- warnings: vitals strip size, templates exist
    n = len(rx.vitals.strip) + (1 if rx.vitals.auto_bmi and compute_bmi(rx.vitals.weight_kg, rx.vitals.height_cm)
                                and not any(c.label.upper() == "BMI" for c in rx.vitals.strip) else 0)
    if n and not 4 <= n <= 7:
        add(Issue(WARN, "vitals_strip_size", f"The vitals strip has {n} columns; 4–7 print best.", "vitals.strip"))
    for kind, ids in (("advice", rx.advice_blocks), ("charts", rx.charts)):
        for tid in ids:
            if not tpl.exists(kind, tid):
                add(Issue(BLOCK, f"template_missing:{kind}:{tid}", f"{kind} template '{tid}' does not exist.", kind))
    for kind, ref in (("diet", rx.diet_plan), ("exercise", rx.exercise_plan)):
        if ref and not tpl.exists(kind, ref.template):
            add(Issue(BLOCK, f"template_missing:{kind}:{ref.template}", f"{kind} template '{ref.template}' does not exist.", f"{kind}_plan"))
    return issues


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _first_strength(name: str) -> Optional[str]:
    m = STRENGTH_RE.search(name or "")
    return m.group(1) if m else None


SPECIALTIES = ["Psychiatry", "Psychiatrist", "Cardiology", "Cardiologist", "Nephrology", "Nephrologist",
               "Gastroenterology", "Gastroenterologist", "Ophthalmology", "Ophthalmologist", "Physiotherapy",
               "Physiotherapist", "Dietitian", "Orthopaedics", "Orthopaedic", "Neurology", "Neurologist",
               "Gynaecology", "Gynaecologist", "Dermatology", "Dermatologist", "Psychology", "Psychologist",
               "Pulmonology", "Pulmonologist", "Urology", "Urologist", "ENT", "Endocrinology", "Endocrinologist",
               "Podiatry", "Podiatrist", "Sleep clinic", "Bariatric surgeon"]
_CANON = {"psychiatrist": "Psychiatry", "cardiologist": "Cardiology", "nephrologist": "Nephrology",
          "gastroenterologist": "Gastroenterology", "ophthalmologist": "Ophthalmology",
          "physiotherapist": "Physiotherapy", "orthopaedic": "Orthopaedics", "neurologist": "Neurology",
          "gynaecologist": "Gynaecology", "dermatologist": "Dermatology", "psychologist": "Psychology",
          "pulmonologist": "Pulmonology", "urologist": "Urology", "endocrinologist": "Endocrinology",
          "podiatrist": "Podiatry"}


def _referrals_in_text(rx: Prescription) -> list[str]:
    texts = []
    if rx.plain_terms:
        texts += rx.plain_terms.paragraphs
    if rx.free_text:
        texts.append(re.sub(r"<[^>]+>", " ", rx.free_text.body_html))
    if rx.follow_up:
        texts += rx.follow_up.bring + rx.follow_up.report_sooner_if
    texts += rx.interval_review + [r.note or "" for _, r in rx.medications.all_rows()] + rx.medications.footnotes
    found = []
    for t in texts:
        for sentence in re.split(r"(?<=[.;!?])\s+", t):
            if not re.search(r"\b(refer|referr|see a|consult|opinion)", sentence, re.I):
                continue
            for s in SPECIALTIES:
                if re.search(rf"\b{re.escape(s)}\b", sentence, re.I):
                    canon = _CANON.get(s.lower(), s)
                    stem = canon[:6].lower()
                    listed = " ".join(r.to.lower() for r in rx.referrals)
                    if stem in listed:
                        continue
                    if canon not in found:
                        found.append(canon)
    return found


def _measurement_issues(rx: Prescription) -> list[Issue]:
    out: list[Issue] = []
    v, p = rx.vitals, rx.patient
    adult = p.age_years is None or p.age_years >= 18

    def warn(code, msg, fld):
        out.append(Issue(WARN, f"implausible:{code}", msg, fld))

    # values from structured fields, else parsed from the strip
    strip = {c.label.strip().upper(): c.value for c in v.strip}

    def num(label, pattern=r"(\d+(?:\.\d+)?)"):
        val = strip.get(label)
        mm = re.search(pattern, val or "")
        return float(mm.group(1)) if mm else None

    waist = v.waist_cm if v.waist_cm is not None else num("WAIST")
    if waist is not None and adult:
        if waist < 60:
            warn("waist", f"Waist {waist:g} cm is under 60 cm for an adult — check the value" +
                 (" (it looks like inches)" if 20 <= waist <= 59 else "") + ".", "vitals")
    height = v.height_cm if v.height_cm is not None else num("HEIGHT")
    if height is not None and adult and height < 100:
        warn("height", f"Height {height:g} cm is implausible for an adult — was it entered in inches or feet?", "vitals.height_cm")
    bmi = compute_bmi(v.weight_kg, v.height_cm)
    if bmi is not None and not 12 <= bmi <= 70:
        warn("bmi", f"BMI {bmi:.1f} is outside 12–70 — check weight and height.", "vitals")
    sys_, dia = v.bp_systolic, v.bp_diastolic
    if sys_ is None and strip.get("BP"):
        mm = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", strip["BP"])
        if mm:
            sys_, dia = int(mm.group(1)), int(mm.group(2))
    if sys_ is not None and dia is not None:
        if not (70 <= sys_ <= 250 and 40 <= dia <= 150) or dia >= sys_:
            warn("bp", f"Blood pressure {sys_}/{dia} mmHg is outside 70/40–250/150 — check the value.", "vitals")
    pulse = v.pulse if v.pulse is not None else num("PULSE")
    if pulse is not None and not 35 <= pulse <= 180:
        warn("pulse", f"Pulse {pulse:g}/min is outside 35–180 — check the value.", "vitals")
    return out


def render_issues(error_message: str) -> list[Issue]:
    return [Issue(BLOCK, "render", error_message, None)]


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == BLOCK]


def unacknowledged(issues: list[Issue], acknowledged: Iterable[str]) -> list[Issue]:
    ack = set(acknowledged)
    return [i for i in issues if i.level == WARN and i.code not in ack]
