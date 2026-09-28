"""Turn a Prescription into an ordered list of atomic HTML chunks (brief §11).

A chunk is never split across pages: it is a section, or one table plus its
bullets. A section's heading always travels with its first chunk. Sections with
no entries produce no chunks at all — never an empty heading, never "N/A".
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from app.config import ASSETS_DIR, Doctor, clinic, get_doctor
from app.rx import templates as tpl
from app.rx.bmi import bmi_cell
from app.rx.clinical import ACTIVE_LISTS, dose_slots, has_molecule, med_lines
from app.rx.render.html_utils import e, sanitise
from app.rx.schema import MedRow, Prescription


class MissingSignature(Exception):
    pass


@dataclass
class Chunk:
    key: str
    label: str
    html: str
    keep_with_next: bool = False
    group: Optional[str] = None  # row-groups of one table share a group id


@dataclass
class LayoutResult:
    chunks: list[Chunk]
    template_versions: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- formatting
def fmt_date(d: date) -> str:
    return f"{d.day:02d} {d:%B %Y}"


def file_date(d: date) -> str:
    return f"{d.day:02d}{d:%b%Y}"


def data_uri(path: Path, mime: str) -> Optional[str]:
    if not path or not path.exists():
        return None
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def section_heading(num: int | None, title: str) -> str:
    n = f'<span class="n">{num}</span>' if num is not None else ""
    return f'<div class="sec-h">{n}<span>{e(title)}</span></div>'


def bullets(items) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return '<ul class="b">' + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"


def table(headers: list[str], rows: list[list[str]], *, numbered: bool = False, cls: str = "t",
          row_classes: list[str] | None = None, raw: bool = False, widths: list[str] | None = None) -> str:
    """Data table with dark header and zebra rows. Cells are escaped unless raw=True."""
    cols = (["#"] if numbered else []) + headers
    colgroup = ""
    if widths:
        colgroup = "<colgroup>" + ("<col style='width:7mm'>" if numbered else "") + "".join(
            f"<col style='width:{w}'>" if w else "<col>" for w in widths) + "</colgroup>"
    head = "<thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in cols) + "</tr></thead>"
    body = []
    for i, r in enumerate(rows):
        rc = f' class="{row_classes[i]}"' if row_classes and row_classes[i] else ""
        cells = "".join(f"<td>{c if raw else e(c)}</td>" for c in r)
        num = f'<td class="num">{i + 1}</td>' if numbered else ""
        body.append(f"<tr{rc}>{num}{cells}</tr>")
    return f'<table class="{cls}">{colgroup}{head}<tbody>{"".join(body)}</tbody></table>'


def box(title: str | None, inner: str, variant: str = "") -> str:
    t = f'<div class="box-t">{e(title)}</div>' if title else ""
    return f'<div class="box {variant}">{t}{inner}</div>'


# ---------------------------------------------------------------- builder
SECTION_ORDER = {
    "standard": ["review", "diagnosis", "labs", "plain_terms", "treatment_options", "investigations",
                 "diet", "exercise", "advice", "medication", "free_text", "referral", "follow_up"],
    "medication_last": ["review", "diagnosis", "labs", "plain_terms", "treatment_options", "investigations",
                        "diet", "exercise", "advice", "free_text", "referral", "follow_up", "medication"],
    "two_page_clinical": ["review", "diagnosis", "labs", "plain_terms", "treatment_options", "investigations",
                          "medication", "free_text", "referral", "follow_up"],
}

DEFAULT_TITLES = {
    "complaints": "COMPLAINTS",
    "interval_review": "INTERVAL REVIEW",
    "diagnosis": "DIAGNOSIS",
    "labs": "LABORATORY FINDINGS",
    "reports": "REPORTS REVIEWED",
    "treatment_options": "TREATMENT OPTIONS",
    "investigations": "INVESTIGATIONS ADVISED",
    "diet": "DIET PLAN",
    "exercise": "EXERCISE PLAN",
    "advice": "ADVICE",
    "medication": "MEDICATION (Rx)",
    "referral": "REFERRAL",
    "follow_up": "FOLLOW-UP",
}

MED_LIST_TITLES = {
    "stopped": "STOP THESE MEDICINES",
    "started": "STARTED / CHANGED",
    "continued": "CONTINUE AS BEFORE",
    "sos": "ONLY IF NEEDED (SOS)",
    "administered": "GIVEN AT THE CLINIC",
    "nutrition": "NUTRITION",
}

# §12 medicine anchors — (formulary class or molecule) -> (anchor kind, printed rule)
ANCHOR_RULES = [
    ({"levothyroxine"}, "on_waking", "on waking, empty stomach"),
    ({"iron_oral"}, "pre_breakfast", "1 hour before breakfast, no tea, coffee or milk"),
    ({"metformin", "sulfonylurea"}, "with_meal", "with the meal"),
    ({"statin"}, "after_dinner", "after dinner"),
]


class LayoutBuilder:
    def __init__(self, rx: Prescription):
        self.rx = rx
        self.clinic = clinic()
        self.num = 0
        self.versions: dict[str, str] = {}
        self.titles = rx.meta.section_titles

    # -- helpers
    def title(self, key: str, default: str | None = None) -> str:
        return self.titles.get(key) or default or DEFAULT_TITLES.get(key, key.upper())

    def next_num(self) -> int:
        self.num += 1
        return self.num

    def template(self, kind: str, tid: str, overrides: dict | None = None) -> dict:
        t = tpl.load_with_overrides(kind, tid, overrides)
        self.versions[f"{kind}/{t['id']}"] = str(t.get("version", "1"))
        return t

    def section(self, key: str, title: str, parts: list) -> list[Chunk]:
        """parts: [(label, html)] or [(label, html, group)] — the heading is glued to the first part."""
        parts = [p if len(p) == 3 else (*p, None) for p in parts if p[1]]
        if not parts:
            return []
        head = section_heading(self.next_num(), title)
        chunks = []
        for i, (lbl, h, group) in enumerate(parts):
            html = (head if i == 0 else "") + h
            chunks.append(Chunk(key, f"{title} — {lbl}" if lbl else title, f'<div class="chunk">{html}</div>', group=group))
        return chunks

    # -- build
    def build(self) -> LayoutResult:
        chunks: list[Chunk] = []
        if self.rx.meta.version > 1 and self.rx.meta.amendment_note:
            chunks.append(Chunk("amendment", "Amendment note", '<div class="chunk">' + box(
                f"AMENDED — VERSION {self.rx.meta.version}", f"<p>{e(self.rx.meta.amendment_note)}</p>", "warn") + "</div>"))
        chunks.extend(self.patient_details())
        order = SECTION_ORDER[self.rx.meta.section_order_preset]
        builders: dict[str, Callable[[], list[Chunk]]] = {
            "review": self.review, "diagnosis": self.diagnosis, "labs": self.labs,
            "plain_terms": self.plain_terms, "treatment_options": self.treatment_options,
            "investigations": self.investigations, "diet": self.diet, "exercise": self.exercise,
            "advice": self.advice, "medication": self.medication, "free_text": self.free_text,
            "referral": self.referral, "follow_up": self.follow_up,
        }
        for key in order:
            chunks.extend(builders[key]())
        chunks.append(self.signature())
        return LayoutResult(chunks, self.versions)

    # -- 0 patient details + vitals
    def patient_details(self) -> list[Chunk]:
        p, m = self.rx.patient, self.rx.meta
        cells: list[tuple[str, str, str]] = [("Name", p.name, "name")]
        age_sex = " / ".join(x for x in [f"{p.age_years} years" if p.age_years is not None else "",
                                          {"M": "Male", "F": "Female", "O": "Other"}.get(p.sex or "", "")] if x)
        if age_sex:
            cells.append(("Age / Sex", age_sex, ""))
        if p.patient_id:
            cells.append(("Patient ID", p.patient_id, ""))
        cells.append(("Date", fmt_date(m.consult_date), ""))
        if p.diet_type:
            cells.append(("Diet type", p.diet_type.capitalize(), ""))
        if p.allergies:
            cells.append(("Allergies", ", ".join(p.allergies), ""))
        if p.known_conditions:
            cells.append(("Known conditions", " · ".join(p.known_conditions), "wide"))
        if p.family_history:
            cells.append(("Family history", " · ".join(p.family_history), "wide"))
        for r in p.extra_rows:
            cells.append((r.label, r.value, "wide" if len(r.value) > 38 else ""))
        # grid rows: half-width cells pair up; a lone half cell before a wide one becomes wide
        grid_rows: list[list[tuple[str, str, str]]] = []
        pending = None
        for lbl, val, cls in cells:
            if "wide" in cls:
                if pending:
                    grid_rows.append([(pending[0], pending[1], pending[2] + " wide")])
                    pending = None
                grid_rows.append([(lbl, val, cls)])
            elif pending:
                grid_rows.append([pending, (lbl, val, cls)])
                pending = None
            else:
                pending = (lbl, val, cls)
        if pending:
            grid_rows.append([(pending[0], pending[1], pending[2] + " wide")])
        grid = "".join(
            f'<div class="cell {cls}{" left" if ci == 0 and len(row) == 2 else ""}{" last" if ri == len(grid_rows) - 1 else ""}">'
            f'<span class="l">{e(lbl)}</span><span class="v">{e(val)}</span></div>'
            for ri, row in enumerate(grid_rows) for ci, (lbl, val, cls) in enumerate(row))
        html = f'<div class="pd">{grid}</div>' + self.vitals_strip()
        return [Chunk("patient", "Patient details", f'<div class="chunk">{html}</div>')]

    def vitals_strip(self) -> str:
        v = self.rx.vitals
        cells = [c.model_dump() for c in v.strip]
        if v.auto_bmi:
            auto = bmi_cell(v.weight_kg, v.height_cm)
            idx = next((i for i, c in enumerate(cells) if c["label"].strip().upper() == "BMI"), None)
            if auto:
                if idx is not None:
                    cells[idx] = {**cells[idx], **auto}
                else:
                    after = max((i for i, c in enumerate(cells) if c["label"].upper() in ("WEIGHT", "HEIGHT")), default=len(cells) - 1)
                    cells.insert(after + 1, {**auto, "earlier": None})
        cells = [c for c in cells if (c.get("value") or "").strip()]  # blank (not yet measured) cells never print
        if not cells:
            return ""
        out = []
        for c in cells:
            sub = f'<span class="sub">{e(c.get("sub"))}</span>' if c.get("sub") else ""
            earlier = f'<span class="earlier">earlier {e(c.get("earlier"))}</span>' if c.get("earlier") else ""
            out.append(f'<div class="c"><div class="h">{e(c["label"].upper())}</div>'
                       f'<div class="val">{e(c["value"])}{sub}{earlier}</div></div>')
        return f'<div class="vit">{"".join(out)}</div>'

    # -- 1 complaints / interval review
    def review(self) -> list[Chunk]:
        out: list[Chunk] = []
        cs = self.rx.complaints
        if cs:
            if any(c.pointer for c in cs):
                body = table(["Complaint", "Pointer"], [[c.text, c.pointer or ""] for c in cs], numbered=True,
                             widths=["45%", None])
            else:
                body = bullets(c.text for c in cs)
            out += self.section("complaints", self.title("complaints"), [("", body)])
        if self.rx.interval_review:
            out += self.section("interval_review", self.title("interval_review"),
                                [("", bullets(self.rx.interval_review))])
        return out

    # -- 2 diagnosis
    def diagnosis(self) -> list[Chunk]:
        ds = self.rx.diagnoses
        if not ds:
            return []
        if len(ds) >= 6 and not any(d.detail for d in ds):
            half = (len(ds) + 1) // 2
            rows = []
            for i in range(half):
                left = f'<b>{i + 1}.</b> {e(ds[i].text)}'
                j = i + half
                right = f'<b>{j + 1}.</b> {e(ds[j].text)}' if j < len(ds) else ""
                rows.append([left, right])
            body = table(["Diagnosis", ""], rows, cls="t two-col", raw=True)
        elif any(d.detail for d in ds):
            body = table(["Diagnosis", "Detail"], [[d.text, d.detail or ""] for d in ds], numbered=True, widths=["50%", None])
        else:
            body = table(["Diagnosis"], [[d.text] for d in ds], numbered=True)
        return self.section("diagnosis", self.title("diagnosis"), [("", body)])

    # -- 3 labs / reports reviewed
    def labs(self) -> list[Chunk]:
        labs = self.rx.labs
        if not labs:
            return []
        has_src = any(l.source or l.date for l in labs)
        has_earlier = any(l.earlier for l in labs)
        headers = ["Test", "Value"] + (["Earlier"] if has_earlier else []) + ["Remark"] + (["Source / date"] if has_src else [])
        rows, rcls = [], []
        for l in labs:
            r = [l.test, l.value] + ([l.earlier or ""] if has_earlier else []) + [l.remark or ""]
            if has_src:
                r.append(" · ".join(x for x in [l.source, l.date] if x))
            rows.append(r)
            rcls.append("hl" if l.highlight else "")
        body = table(headers, rows, numbered=True, row_classes=rcls)
        if self.rx.labs_footnote:
            body += f'<div class="note">{e(self.rx.labs_footnote)}</div>'
        default = DEFAULT_TITLES["reports"] if has_src else DEFAULT_TITLES["labs"]
        return self.section("labs", self.title("labs", default), [("", body)])

    # -- 4 plain terms
    def plain_terms(self) -> list[Chunk]:
        pt = self.rx.plain_terms
        if not pt or not pt.paragraphs:
            return []
        html = box(pt.title, "".join(f"<p>{e(p)}</p>" for p in pt.paragraphs))
        return [Chunk("plain_terms", "Plain-terms box", f'<div class="chunk">{html}</div>')]

    # -- 5 treatment options
    def treatment_options(self) -> list[Chunk]:
        to = self.rx.treatment_options
        if not to or not to.rows:
            return []
        body = f"<p>{e(to.intro)}</p>" if to.intro else ""
        body += table([""] + to.columns, [[r.label, *r.values] for r in to.rows])
        if to.recommendation:
            body += '<div style="height:2mm"></div>' + box("RECOMMENDATION", f"<p>{e(to.recommendation)}</p>")
        return self.section("treatment_options", self.title("treatment_options"), [("", body)])

    # -- 6 investigations
    def investigations(self) -> list[Chunk]:
        inv = self.rx.investigations
        if not inv:
            return []
        groups: dict[str, list] = {}
        for i in inv:
            groups.setdefault(i.timing or "", []).append(i)
        parts = []
        for timing, items in groups.items():
            sub = f'<div class="sub-h teal">{e(timing.upper())}</div>' if timing else ""
            if not any(i.instruction or i.purpose for i in items) and len(items) >= 6:
                half = (len(items) + 1) // 2
                rows = [[f"<b>{k + 1}.</b> {e(items[k].name)}",
                         f"<b>{k + half + 1}.</b> {e(items[k + half].name)}" if k + half < len(items) else ""]
                        for k in range(half)]
                body = table(["Test", ""], rows, cls="t two-col", raw=True)
            else:
                has_purpose = any(i.purpose for i in items)
                headers = ["Test"] + (["Purpose"] if has_purpose else []) + ["Instructions"]
                rows = [[i.name] + ([i.purpose or ""] if has_purpose else []) + [i.instruction or ""] for i in items]
                body = table(headers, rows, numbered=True, widths=["38%"] + (["25%"] if has_purpose else []) + [None])
            parts.append((timing, sub + body))
        return self.section("investigations", self.title("investigations"), parts)

    # -- 7 diet
    def diet(self) -> list[Chunk]:
        ref = self.rx.diet_plan
        if not ref:
            return []
        t = self.template("diet", ref.template, ref.overrides)
        title = ref.title_override or self.titles.get("diet") or t.get("title") or DEFAULT_TITLES["diet"]
        anchors = self.medicine_anchors() if self.rx.meta.medicine_anchors else {}
        parts = []
        tg = t.get("target") or {}
        target = " · ".join(f"<b>{e(k.capitalize() if k != 'kcal' else 'Energy')}:</b> {e(v)}" for k, v in tg.items() if v)
        intro = f'<div class="box" style="padding:1.6mm 3mm;margin-bottom:2mm">{target}</div>' if target else ""
        if t.get("meal_pattern"):
            intro += f'<div class="small" style="margin-bottom:1.2mm">{e(t["meal_pattern"])}</div>'
        slots = t.get("slots") or []
        if slots:
            rows = []
            for s in slots:
                opts = s.get("options") or []
                letters = "abcdefgh"
                li = "".join(f"<li><b>{letters[i]}.</b>{e(o)}</li>" for i, o in enumerate(opts)) if len(opts) > 1 else \
                    "".join(f"<li>{e(o)}</li>" for o in opts)
                anc = "".join(f'<div class="anchor">{e(a)}</div>' for a in anchors.get(s.get("key"), []))
                rows.append([f"{e(s.get('time'))}", f'<ul class="opts">{li}</ul>{anc}'])
            # row-groups of 2 slots: each prints with its own header if it starts a page,
            # and re-joins the previous group when they share a page
            limit = 2
            for n, start in enumerate(range(0, len(rows), limit)):
                part = table(["Time", "Options — choose one"], rows[start:start + limit], raw=True, widths=["30mm", None])
                parts.append((f"meal slots {start + 1}–{min(start + limit, len(rows))}", (intro if n == 0 else "") + part, "diet-slots"))
        elif intro:
            parts.append(("targets", intro))
        ia = t.get("include_avoid")
        if ia:
            inc, avo = ia.get("include") or [], ia.get("avoid") or []
            n = max(len(inc), len(avo))
            rows = [[inc[i] if i < len(inc) else "", avo[i] if i < len(avo) else ""] for i in range(n)]
            parts.append(("include / avoid", table(["Include", ia.get("avoid_title", "Avoid or limit")], rows, cls="t two-col")))
        dd = t.get("dos_and_donts")
        if dd:
            parts.append(("do's and don'ts", '<div class="two">'
                          f'<div>{box("DO", bullets(dd.get("do") or []))}</div>'
                          f'<div>{box("DON’T", bullets(dd.get("dont") or []), "stop")}</div></div>'))
        sw = t.get("simple_swaps")
        if sw:
            parts.append(("simple swaps", '<div class="sub-h">SIMPLE SWAPS</div>' + table(
                ["Replace", "With"], [[s["replace"], s["with"]] for s in sw], cls="t two-col")))
        cb = t.get("closing_bullets")
        if cb:
            parts.append(("closing notes", bullets(cb)))
        return self.section("diet", title, parts)

    def medicine_anchors(self) -> dict[str, list[str]]:
        """slot key -> grey anchor lines, from food-linked medicine timing (§12)."""
        out: dict[str, list[str]] = {}
        for ml in med_lines(self.rx, active_only=True):
            if ml.list_name not in ("started", "continued"):
                continue
            tags = set(ml.res.classes) | set(ml.res.molecules)
            if "iron" in tags and "ferric carboxymaltose" not in ml.res.molecules:
                tags.add("iron_oral")
            for keys, kind, rule in ANCHOR_RULES:
                if not keys & tags:
                    continue
                line = f"{ml.row.name} — {rule}"
                if kind == "on_waking":
                    out.setdefault("on_waking", []).append(line)
                elif kind == "pre_breakfast":
                    out.setdefault("breakfast", []).append(line)
                elif kind == "after_dinner":
                    out.setdefault("dinner", []).append(line)
                elif kind == "with_meal":
                    slots = dose_slots(ml.row.dose) or (True, False, False)
                    for on, slot in zip(slots, ("breakfast", "lunch", "dinner")):
                        if on:
                            out.setdefault(slot, []).append(line)
                break
        return out

    # -- 8 exercise
    def exercise(self) -> list[Chunk]:
        ref = self.rx.exercise_plan
        if not ref:
            return []
        t = self.template("exercise", ref.template, ref.overrides)
        title = ref.title_override or self.titles.get("exercise") or t.get("title") or DEFAULT_TITLES["exercise"]
        parts = []
        wk = t.get("weekly") or []
        if wk:
            intro = f"<p>{e(t['intro'])}</p>" if t.get("intro") else ""
            rows = [[d["day"], d["activity"], d.get("duration", "")] for d in wk]
            for n, start in enumerate(range(0, len(rows), 4)):
                parts.append((f"weekly plan {n + 1}", (intro if n == 0 else "") + table(
                    ["Day", "Exercise", "Duration"], rows[start:start + 4], widths=["22mm", None, "32mm"]), "ex-weekly"))
        pr = t.get("progression")
        if pr:
            parts.append(("progression", f'<div class="sub-h">{e(pr.get("title", "PROGRESSION"))}</div>' +
                          table([""] + pr["columns"], [[r["label"], *r["values"]] for r in pr["rows"]])))
        tail = ""
        if t.get("precautions"):
            tail += box(t.get("precautions_title", "PRECAUTIONS"), bullets(t["precautions"]), "warn")
        if t.get("physiotherapy"):
            tail += box("PHYSIOTHERAPY", bullets(t["physiotherapy"]))
        if t.get("protein_timing"):
            tail += f'<div class="note"><b>Protein timing:</b> {e(t["protein_timing"])}</div>'
        if tail:
            parts.append(("precautions", tail))
        return self.section("exercise", title, parts)

    # -- 9 advice + charts
    def advice(self) -> list[Chunk]:
        parts = []
        glp1_supply = has_molecule(self.rx, "tirzepatide", "semaglutide")
        for bid in self.rx.advice_blocks:
            t = self.template("advice", bid)
            parts.append((t.get("title", bid), self.advice_html(t, glp1_supply)))
        for cid in self.rx.charts:
            t = self.template("charts", cid)
            parts.append((t.get("title", cid), self.chart_html(t)))
        return self.section("advice", self.title("advice", "ADVICE · DO'S AND DON'TS · CHARTS"), parts)

    def advice_html(self, t: dict, glp1_supply: bool) -> str:
        inner = f"<p>{e(t['intro'])}</p>" if t.get("intro") else ""
        if t.get("columns"):
            inner += '<div class="two">' + "".join(
                f'<div><div class="sub-h {"do-h" if i == 0 else "dont-h"}">{e(c["title"])}</div>{bullets(c["bullets"])}</div>'
                for i, c in enumerate(t["columns"])) + "</div>"
        inner += bullets(t.get("bullets") or [])
        if t.get("footer"):
            inner += f'<div class="note">{e(t["footer"])}</div>'
        if t.get("supply_contact") and glp1_supply:
            sc = (self.clinic.get("supply_contacts") or {}).get(t["supply_contact"])
            if sc:
                inner += f'<p style="margin-top:1.4mm"><b>{e(sc["label"])}:</b> {e(sc["name"])}: {e(sc["number"])}</p>'
        return box(t.get("title"), inner, t.get("style", ""))

    def chart_html(self, t: dict) -> str:
        cols = t.get("columns") or []
        rows = t.get("rows") or [str(i + 1) for i in range(int(t.get("row_count", 7)))]
        head = "<tr><th>" + e(t.get("row_header", "Day")) + "</th>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>"
        body = "".join('<tr><td class="lab">' + e(r) + "</td>" + "<td></td>" * len(cols) + "</tr>" for r in rows)
        html = f'<div class="sub-h">{e(t.get("title"))}</div><table class="chart">{head}{body}</table>'
        if t.get("target"):
            html += f'<div class="chart-target">Target: {e(t["target"])}</div>'
        html += bullets(t.get("instructions") or [])
        return html

    # -- 10 medication
    def med_table(self, rows: list[MedRow], list_name: str) -> str:
        has_dur = any(r.duration for r in rows)
        headers = ["Medicine", "Dose & timing"] + (["Duration"] if has_dur else []) + ["Note / purpose"]
        out = []
        for r in rows:
            med = f'<span class="brand">{e(r.name)}</span>' + (f'<span class="gen">{e(r.generic)}</span>' if r.generic else "")
            dose = " · ".join(x for x in [r.dose, r.timing, r.route] if x) or ("SOS" if list_name == "sos" else "")
            note = " ".join(x for x in [f"Start after {r.start_after}." if r.start_after else "", r.note or "",
                                        r.purpose or ""] if x)
            out.append([med, e(dose)] + ([e(r.duration or "")] if has_dur else []) + [e(note)])
        return table(headers, out, numbered=True, raw=True, widths=["34%", "22%"] + (["14%"] if has_dur else []) + [None])

    def medication(self) -> list[Chunk]:
        meds = self.rx.medications
        parts = []
        if meds.stopped:
            items = [f"{r.name}" + (f" — {r.reason}" if r.reason else "") for r in meds.stopped]
            parts.append(("stop", box(MED_LIST_TITLES["stopped"], bullets(items), "stop")))
        for ln in ACTIVE_LISTS:
            rows = getattr(meds, ln)
            if rows:
                parts.append((ln, f'<div class="sub-h teal">{e(self.titles.get("med_" + ln, MED_LIST_TITLES[ln]))}</div>'
                              + self.med_table(rows, ln)))
        if self.rx.taper_plans:
            parts.append(("taper", box("TAPER PLAN", bullets(f"{t.medicine}: {t.instruction}" for t in self.rx.taper_plans), "warn")))
        if meds.footnotes:
            parts.append(("footnotes", "".join(f'<div class="note">* {e(f)}</div>' for f in meds.footnotes)))
        return self.section("medication", self.title("medication"), parts)

    # -- free text
    def free_text(self) -> list[Chunk]:
        ft = self.rx.free_text
        if not ft or not ft.body_html.strip():
            return []
        return self.section("free_text", ft.section_title, [("", sanitise(ft.body_html))])

    # -- 11 referral
    def referral(self) -> list[Chunk]:
        rf = self.rx.referrals
        if not rf:
            return []
        rows = [[r.to, r.reason or "", r.named_doctor or "", r.contact or ""] for r in rf]
        return self.section("referral", self.title("referral"), [("", table(["Refer to", "Reason", "Doctor", "Contact"], rows, numbered=True))])

    # -- 12 follow-up
    def follow_up(self) -> list[Chunk]:
        fu = self.rx.follow_up
        contacts = list(self.rx.contacts)
        if not fu and not contacts:
            return []
        inner = ""
        if fu and fu.interval:
            inner += f"<p><b>Next visit:</b> after {e(fu.interval)}</p>"
        if fu and fu.bring:
            inner += "<p><b>Please bring:</b> " + "; ".join(e(x) for x in fu.bring) + "</p>"
        if fu and fu.report_sooner_if:
            inner += "<p><b>Contact us sooner if you notice:</b> " + "; ".join(e(x) for x in fu.report_sooner_if) + "</p>"
        for c in contacts:
            inner += f"<p><b>{e(c.label)}:</b> " + " · ".join(e(x) for x in [c.name, c.number] if x) + "</p>"
        coord = self.clinic.get("coordinator_contact") or {}
        if coord.get("number"):
            inner += f"<p><b>{e(coord['label'])}:</b> {e(coord.get('name'))} · {e(coord['number'])}</p>"
        return self.section("follow_up", self.title("follow_up"), [("", box(None, inner))])

    # -- signature
    def signature(self) -> Chunk:
        m = self.rx.meta
        blocks = [signature_block(get_doctor(m.seen_by))] if m.seen_by else []
        if m.co_signatory:
            blocks.append(signature_block(get_doctor(m.co_signatory)))
        meta = f'<div class="sig-meta">{e(self.clinic.get("place", ""))} · {e(fmt_date(m.consult_date))}</div>'
        html = f'<div class="chunk">{meta}<div class="sig-wrap">{"".join(blocks)}</div></div>'
        return Chunk("signature", "Signature block", html)


def signature_block(doc: Doctor) -> str:
    img = ""
    if doc.sign_style == "image":
        uri = data_uri(doc.signature_path(), "image/png")
        if not uri:
            raise MissingSignature(f"signature image for {doc.sign_name} not found at {doc.signature_path()}")
        img = f'<img src="{uri}" alt="Signature of {e(doc.sign_name)}">'
    else:
        img = '<div class="sig-space"></div>'
    sub = "".join(f'<div class="sig-sub">{e(s)}</div>' for s in doc.sign_sub)
    reg = f'<div class="sig-sub">Reg. No. {e(doc.registration_no)}</div>' if doc.registration_no else ""
    return f'<div class="sig {e(doc.sign_style)}" data-doctor="{e(doc.key)}">{img}<div class="sig-name">{e(doc.sign_name)}</div>{sub}{reg}</div>'


def build_layout(rx: Prescription) -> LayoutResult:
    return LayoutBuilder(rx).build()


def logo_uri() -> Optional[str]:
    for name, mime in (("logo.svg", "image/svg+xml"), ("logo.png", "image/png")):
        uri = data_uri(ASSETS_DIR / name, mime)
        if uri:
            return uri
    return None
