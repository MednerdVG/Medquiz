"""HTML + CSS -> PDF through headless Chromium (Playwright).

Pipeline:
  1. layout  — prescription -> atomic chunks
  2. measure — render every chunk in a column exactly as wide as the page body and
               read its height; read the real body capacity of page 1 and page 2+
  3. pack    — dynamic-programming packer assigns chunks to pages
  4. compose — pages with letterhead, strips and `Page n of N` footer
  5. verify  — post-render: each page's last element must end above the footer and
               each page must carry the patient's name; otherwise fail loudly
  6. print   — A4, zero margins, backgrounds on; metadata normalised so the same
               input always yields byte-identical PDFs
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import ASSETS_DIR, clinic, get_doctor
from app.rx.render import cert as certs
from app.rx.render.html_utils import e
from app.rx.render.layout import Chunk, MissingSignature, build_layout, fmt_date, logo_uri
from app.rx.templates import TemplateNotFound
from app.rx.render.packer import ChunkTooTall, PackItem, pack
from app.rx.schema import Prescription

CSS_PATH = Path(__file__).resolve().parent / "css" / "rx.css"
PX_PER_MM = 96 / 25.4
FONT_FACES = [("Lato-Regular.ttf", 400, "normal"), ("Lato-Italic.ttf", 400, "italic"),
              ("Lato-Bold.ttf", 700, "normal"), ("Lato-BoldItalic.ttf", 700, "italic"),
              ("Lato-Black.ttf", 900, "normal")]


class RenderError(Exception):
    """Raised instead of shipping a clipped or unsafe page."""


@dataclass
class RenderResult:
    pdf: bytes
    html: str
    page_count: int
    template_versions: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdf).hexdigest()


# ---------------------------------------------------------------- assets
_css_cache: Optional[str] = None


def stylesheet() -> str:
    global _css_cache
    if _css_cache is None:
        import base64
        faces = []
        for fname, weight, style in FONT_FACES:
            p = ASSETS_DIR / "fonts" / fname
            if p.exists():
                b64 = base64.b64encode(p.read_bytes()).decode()
                faces.append(f'@font-face{{font-family:"Lato";font-weight:{weight};font-style:{style};'
                             f'src:url(data:font/ttf;base64,{b64}) format("truetype");}}')
        _css_cache = "\n".join(faces) + "\n" + CSS_PATH.read_text(encoding="utf-8")
    return _css_cache


# ---------------------------------------------------------------- page furniture
def letterhead(doctor_key: str | None) -> str:
    c = clinic()
    logo = logo_uri()
    brand = (f'<img class="lh-logo" src="{logo}" alt="">' if logo else
             '<div><div class="lh-word">META<span>FIX</span></div>'
             f'<div class="lh-tag">{e(c.get("tagline", ""))}</div></div>')
    doc_html = ""
    if doctor_key:
        d = get_doctor(doctor_key)
        doc_html = (f'<div class="lh-doc-name">{e(d.lh_name)}</div>'
                    f'<div class="lh-doc-deg">{e(d.lh_degrees)}</div>'
                    + (f'<div class="lh-doc-spec">{e(d.letterhead_specialty)}</div>' if d.letterhead_specialty else "")
                    + (f'<div class="lh-doc-reg">Reg. No. {e(d.registration_no)}</div>' if d.registration_no else ""))
    return (f'<div class="lh"><div class="lh-brand">{brand}</div><div class="lh-doc">{doc_html}</div></div>'
            f'<div class="member-bar">{e(c.get("member_bar", ""))}</div>')


def footer(n: int, total: int) -> str:
    c = clinic()
    contact = " · ".join(e(x) for x in [c.get("phone"), c.get("email"), c.get("website"), c.get("handles")] if x)
    return (f'<div class="ft"><div class="ft-row"><span>{e(c.get("address", ""))}</span>'
            f'<span class="ft-page">Page {n} of {total}</span></div>'
            f'<div class="ft-row"><span>{contact}</span><span class="ft-tag">{e(c.get("tagline", ""))}</span></div></div>')


def doc_type_label(rx: Prescription) -> tuple[str, str]:
    m = rx.meta
    if m.consult_type == "follow_up" or m.visit_number > 1:
        sub = f"FOLLOW-UP {max(m.visit_number - 1, 1)}"
    elif m.consult_type == "teleconsult":
        sub = "TELECONSULTATION"
    else:
        sub = "FIRST VISIT"
    if m.version > 1:
        sub += f" · AMENDED v{m.version}"
    title = "CLINICAL SUMMARY" if m.section_order_preset == "two_page_clinical" else "PRESCRIPTION"
    return title, sub


def top_strip(title: str, sub: str, when: str) -> str:
    return (f'<div class="top-strip"><div class="doc-type">{e(title)}<small>{e(sub)}</small></div>'
            f'<div class="doc-date">{e(when)}</div></div>')


def cont_strip(patient_name: str, when: str) -> str:
    return f'<div class="cont-strip">{e(patient_name.upper())} · {e(when.upper())} · CONTINUED</div>'


def page_html(inner: str, head: str, strip: str, n: int, total: int, kind: str = "rx") -> str:
    return (f'<section class="page" data-page="{n}" data-kind="{kind}">{head}{strip}'
            f'<div class="page-body"><div class="page-body-inner">{inner}</div></div>{footer(n, total)}</section>')


def document(pages: list[str], extra: str = "") -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Metafix Prescription</title>'
            f"<style>{stylesheet()}</style></head><body>{''.join(pages)}{extra}</body></html>")


# ---------------------------------------------------------------- browser
class _Browser:
    """One Chromium per render call: thread-safe and leaves no state between patients."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        exe = os.environ.get("CHROMIUM_EXECUTABLE")
        kwargs = {"args": ["--font-render-hinting=none", "--disable-lcd-text"]}
        try:
            self.browser = self._pw.chromium.launch(**kwargs, **({"executable_path": exe} if exe else {}))
        except Exception:
            fallback = "/opt/pw-browsers/chromium"
            if exe or not Path(fallback).exists():
                self._pw.stop()
                raise
            self.browser = self._pw.chromium.launch(executable_path=_find_chrome(fallback), **kwargs)
        self.context = self.browser.new_context(viewport={"width": 794, "height": 1123}, device_scale_factor=1,
                                                java_script_enabled=True, offline=True)
        self.page = self.context.new_page()
        return self

    def load(self, html: str):
        self.page.set_content(html, wait_until="load")
        self.page.evaluate("document.fonts.ready.then(() => true)")
        self.page.emulate_media(media="print")

    def __exit__(self, *exc):
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()


def _find_chrome(root: str) -> str:
    p = Path(root)
    if p.is_file():
        return str(p)
    for cand in ("chrome-linux/chrome", "chrome-linux64/chrome", "chrome"):
        if (p / cand).exists():
            return str(p / cand)
    return root


MEASURE_JS = """
() => {
  const px = %f;
  const q = s => Array.from(document.querySelectorAll(s));
  const ms = q('.measure > .m');
  const heights = ms.map(el => el.getBoundingClientRect().height / px);
  // grouped chunks: height when joined to the previous group member, minus the gap the
  // previous member gives up (its padding-bottom)
  const joined = ms.map(el => {
    const c = el.firstElementChild;
    if (!c || !c.dataset.group) return null;
    const pad = parseFloat(getComputedStyle(c).paddingBottom) / px;
    c.classList.add('joined');
    const h = el.getBoundingClientRect().height / px;
    c.classList.remove('joined');
    return h - pad;
  });
  const caps = q('section.page').map(p => {
    const b = p.querySelector('.page-body');
    const cs = getComputedStyle(b);
    return (b.getBoundingClientRect().height - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)) / px;
  });
  return {heights, joined, caps};
}
""" % PX_PER_MM

VERIFY_JS = """
(patientName) => {
  const px = %f;
  const out = [];
  document.querySelectorAll('section.page').forEach(p => {
    const n = +p.dataset.page;
    const body = p.querySelector('.page-body');
    const inner = p.querySelector('.page-body-inner');
    const ft = p.querySelector('.ft');
    const limit = Math.min(ft.getBoundingClientRect().top,
                           body.getBoundingClientRect().bottom - parseFloat(getComputedStyle(body).paddingBottom));
    let bottom = inner.getBoundingClientRect().top;
    inner.querySelectorAll('*').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.height > 0 && r.bottom > bottom) bottom = r.bottom;
    });
    const text = p.innerText.toUpperCase();
    out.push({n, over_mm: (bottom - limit) / px, has_name: text.includes(patientName.toUpperCase()),
              label: (inner.lastElementChild && inner.lastElementChild.dataset.label) || ''});
  });
  return out;
}
""" % PX_PER_MM


# ---------------------------------------------------------------- render
def render(rx: Prescription, *, check_name: bool = True) -> RenderResult:
    if not rx.meta.seen_by:
        raise RenderError("seen_by is required — no prescription is rendered without the treating doctor")
    if not rx.patient.name.strip():
        raise RenderError("patient name is required")
    try:
        layout = build_layout(rx)
    except (MissingSignature, KeyError, TemplateNotFound) as exc:
        raise RenderError(str(exc)) from exc
    chunks = layout.chunks
    when = fmt_date(rx.meta.consult_date)
    title, sub = doc_type_label(rx)
    head = letterhead(rx.meta.seen_by)
    strip1 = top_strip(title, sub, when)
    strip2 = cont_strip(rx.patient.name, when)
    try:
        cert_pages = certs.build_certificate_pages(rx)
    except (ValueError, KeyError) as exc:
        raise RenderError(f"certificate: {exc}") from exc

    with _Browser() as br:
        # 2. measure
        skeleton = [page_html("", head, strip1, 1, 1), page_html("", head, strip2, 2, 2)]
        measure = '<div class="measure">' + "".join(
            f'<div class="m">{_grouped(c)}</div>' for c in chunks) + "</div>"
        br.load(document(skeleton, measure))
        m = br.page.evaluate(MEASURE_JS)
        cap1, cap2 = m["caps"][0] - 0.5, m["caps"][1] - 0.5  # half-mm safety for sub-pixel rounding
        items = [PackItem(c.label, h, c.keep_with_next, c.group, jh)
                 for c, h, jh in zip(chunks, m["heights"], m["joined"])]
        # 3. pack — the signature block should not sit alone on a page: glue it to what precedes
        if len(items) >= 2:
            items[-2].keep_with_next = True
        try:
            pages_idx = pack(items, cap1, cap2)
        except ChunkTooTall as exc:
            raise RenderError(str(exc)) from exc

        # 4. compose
        total = len(pages_idx) + len(cert_pages)
        pages = []
        for pi, idx in enumerate(pages_idx):
            inner = "".join(_labelled(chunks[i], idx, chunks) for i in idx)
            pages.append(page_html(inner, head, strip1 if pi == 0 else strip2, pi + 1, total))
        for ci, cp in enumerate(cert_pages):
            n = len(pages_idx) + ci + 1
            pages.append(page_html(cp.html, letterhead(cp.doctor_key), top_strip(cp.title, cp.subtitle, f"{rx.patient.name} · {when}"),
                                   n, total, kind="cert"))
        html = document(pages)
        br.load(html)

        # 5. verify
        problems = []
        for r in br.page.evaluate(VERIFY_JS, rx.patient.name):
            if r["over_mm"] > 0.2:
                problems.append(f"page {r['n']} overflows the footer by {r['over_mm']:.1f} mm"
                                + (f" (last block: {r['label']})" if r["label"] else ""))
            if check_name and not r["has_name"]:
                problems.append(f"page {r['n']} does not carry the patient's name")
        if problems:
            raise RenderError("Render rejected — " + "; ".join(problems))

        # 6. print
        pdf = br.page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                          margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
    pdf = normalise_pdf(pdf, rx)
    return RenderResult(pdf=pdf, html=html, page_count=total, template_versions=layout.template_versions)


def _grouped(c: Chunk) -> str:
    if not c.group:
        return c.html
    return c.html.replace('<div class="chunk">', f'<div class="chunk" data-group="{e(c.group)}">', 1)


def _labelled(c: Chunk, page_idx: list[int] | None = None, chunks: list[Chunk] | None = None) -> str:
    """Tag the chunk so overflow errors can name the block; mark row-group joins on the same page."""
    cls = "chunk"
    if page_idx is not None and chunks is not None and c.group:
        pos = next(k for k, i in enumerate(page_idx) if chunks[i] is c)
        if pos > 0 and chunks[page_idx[pos - 1]].group == c.group:
            cls += " joined"
        if pos + 1 < len(page_idx) and chunks[page_idx[pos + 1]].group == c.group:
            cls += " gprev"
    return c.html.replace('<div class="chunk">', f'<div class="{cls}" data-label="{e(c.label)}">', 1)


_DATE_RE = re.compile(rb"/(CreationDate|ModDate) \(D:[^)]*\)")


def normalise_pdf(pdf: bytes, rx: Prescription) -> bytes:
    """Replace volatile metadata with values derived from the document, keeping byte lengths
    identical so the xref table stays valid. Same input -> byte-identical PDF."""
    stamp = rx.meta.consult_date.strftime("%Y%m%d") + "000000"

    def sub(mo: re.Match) -> bytes:
        orig = mo.group(0)
        key = mo.group(1)
        inner_len = len(orig) - len(b"/" + key + b" (D:)")
        fixed = (stamp + "Z").encode()[:inner_len].ljust(inner_len, b"0")
        return b"/" + key + b" (D:" + fixed + b")"

    return _DATE_RE.sub(sub, pdf)


def file_name(rx: Prescription) -> str:
    from app.rx.render.layout import file_date
    safe = re.sub(r"[^A-Za-z0-9]+", "_", rx.patient.name.strip()).strip("_")
    v = f"_v{rx.meta.version}" if rx.meta.version > 1 else ""
    return f"Metafix_Prescription_{safe}_{file_date(rx.meta.consult_date)}{v}.pdf"
