"""Certificate pages (brief §13).

Each certificate prints on its own page inside a bordered `cert` frame beneath the
clinic letterhead. The signature area is a blank ruled space above the doctor's
full legal name, qualifications and "Signature & seal", beside a dashed clinic
stamp box. Nothing is pre-signed: no signature image is ever placed here.
"""
from __future__ import annotations

from dataclasses import dataclass

from jinja2 import Environment, StrictUndefined

from app.config import clinic, get_doctor
from app.rx import templates as tpl
from app.rx.render.html_utils import e
from app.rx.schema import CertificateRequest, Prescription

_env = Environment(autoescape=True, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


@dataclass
class CertPage:
    html: str
    title: str
    subtitle: str
    doctor_key: str


def _fmt(d) -> str:
    from app.rx.render.layout import fmt_date
    return fmt_date(d)


def cert_signature(doctor_key: str) -> str:
    d = get_doctor(doctor_key)
    quals = "".join(f'<div class="q">{e(s)}</div>' for s in d.sign_sub[:1])
    reg = f'<div class="q">Reg. No. {e(d.registration_no)}</div>' if d.registration_no else ""
    return ('<div class="cert-sign">'
            '<div class="stamp">Clinic stamp</div>'
            f'<div class="cert-sig"><div class="ruled"></div><div class="nm">{e(d.full_legal_name)}</div>'
            f'{quals}{reg}<div class="ss">Signature &amp; seal</div></div></div>')


def _context(rx: Prescription, req: CertificateRequest) -> dict:
    d = get_doctor(rx.meta.seen_by)
    p = rx.patient
    pronoun = {"M": ("Mr.", "he", "his"), "F": ("Ms.", "she", "her")}.get(p.sex or "", ("", "they", "their"))
    ctx = {
        "patient_name": p.name, "age": p.age_years, "sex": p.sex, "patient_id": p.patient_id,
        "they": pronoun[1], "their": pronoun[2],
        "doctor_name": d.full_legal_name, "doctor_short": d.sign_name,
        "conditions": [x.text for x in rx.diagnoses],
        "place": clinic().get("place", ""), "issue_date": _fmt(rx.meta.consult_date),
        "consult_date": _fmt(rx.meta.consult_date),
    }
    ctx.update(req.fields)
    return ctx


def _render_body(t: dict, ctx: dict) -> str:
    parts = []
    for para in t.get("body") or []:
        txt = _env.from_string(para).render(**ctx).strip()
        if txt:
            parts.append(f"<p>{txt}</p>")
    return "".join(parts)


def build_certificate_pages(rx: Prescription) -> list[CertPage]:
    pages = []
    for req in rx.certificate_requests():
        if req.type == "insurance_summary_letter":
            pages.extend(_insurance_pages(rx, req))
            continue
        t = tpl.load("certificates", req.type)
        ctx = {**{k: v for k, v in (t.get("defaults") or {}).items()}, **_context(rx, req)}
        missing = [f for f in t.get("required_fields") or [] if not ctx.get(f)]
        if missing:
            raise ValueError(f"{req.type}: missing field(s) {', '.join(missing)}")
        body = _render_body(t, ctx)
        meta = (f'<div class="cert-meta"><div><b>Place:</b> {e(ctx["place"])}</div>'
                f'<div><b>Date of issue:</b> {e(ctx.get("issue_date"))}</div></div>')
        html = (f'<div class="chunk" data-label="{e(t["title"])}"><div class="cert"><div class="cert-title">{e(t["title"])}</div>'
                f'<div class="cert-rule"></div><div class="cert-body">{body}</div>{meta}'
                f'{cert_signature(rx.meta.seen_by)}</div></div>')
        pages.append(CertPage(html, "CERTIFICATE", t.get("strip", ""), rx.meta.seen_by))
    return pages


def _insurance_pages(rx: Prescription, req: CertificateRequest) -> list[CertPage]:
    """Two-page clinical summary for insurers (preset two_page_clinical)."""
    from app.rx.render.layout import bullets, table
    t = tpl.load("certificates", "insurance_summary_letter")
    ctx = _context(rx, req)
    intro = _render_body(t, ctx)
    p1 = [intro]
    if rx.diagnoses:
        p1.append('<div class="sub-h">DIAGNOSIS</div>' + table(["Diagnosis"], [[d.text] for d in rx.diagnoses], numbered=True))
    if rx.labs:
        p1.append('<div class="sub-h" style="margin-top:3mm">KEY INVESTIGATIONS</div>' +
                  table(["Test", "Value", "Remark"], [[l.test, l.value, l.remark or ""] for l in rx.labs], numbered=True))
    p2 = []
    active = [r for k in ("started", "continued", "administered") for r in getattr(rx.medications, k)]
    if active:
        p2.append('<div class="sub-h">CURRENT TREATMENT</div>' + table(
            ["Medicine", "Dose & timing", "Duration"],
            [[r.name + (f" ({r.generic})" if r.generic else ""), " · ".join(x for x in [r.dose, r.timing, r.route] if x),
              r.duration or ""] for r in active], numbered=True))
    if rx.investigations:
        p2.append('<div class="sub-h" style="margin-top:3mm">PLAN — INVESTIGATIONS</div>' + bullets(i.name for i in rx.investigations))
    if rx.follow_up and rx.follow_up.interval:
        p2.append(f'<p style="margin-top:3mm"><b>Review:</b> after {e(rx.follow_up.interval)}</p>')
    closing = "".join(f"<p>{_env.from_string(c).render(**ctx)}</p>" for c in t.get("closing") or [])
    frame = lambda inner, sign: (  # noqa: E731
        f'<div class="chunk" data-label="{e(t["title"])}"><div class="cert" style="min-height:0">'
        f'<div class="cert-title">{e(t["title"])}</div><div class="cert-rule"></div>'
        f'<div class="cert-body" style="font-size:9.6pt;line-height:1.5">{inner}</div>{sign}</div></div>')
    meta = (f'<div class="cert-meta"><b>Place:</b> {e(ctx["place"])} · <b>Date of issue:</b> {e(ctx["issue_date"])}</div>')
    return [
        CertPage(frame("".join(p1), ""), "CLINICAL SUMMARY", "FOR INSURANCE · PAGE 1 OF 2", rx.meta.seen_by),
        CertPage(frame("".join(p2) + closing, meta + cert_signature(rx.meta.seen_by)), "CLINICAL SUMMARY",
                 "FOR INSURANCE · PAGE 2 OF 2", rx.meta.seen_by),
    ]
