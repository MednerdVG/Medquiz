"""Render a prescription JSON to PDF + one PNG per page (for eyeballing layout).

    python -m tools.preview examples/first_visit.json out/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.rx.render.renderer import _Browser, file_name, render
from app.rx.schema import Prescription


def main(src: str, out_dir: str, scale: float = 0.8) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rx = Prescription.model_validate(json.loads(Path(src).read_text(encoding="utf-8")))
    res = render(rx)
    pdf_path = out / file_name(rx)
    pdf_path.write_bytes(res.pdf)
    with _Browser() as br:
        br.page.set_viewport_size({"width": 794, "height": 1123})
        br.load(res.html)
        for i, el in enumerate(br.page.query_selector_all("section.page"), 1):
            el.screenshot(path=str(out / f"page-{i}.png"), scale="css")
    print(f"{pdf_path}  ({res.page_count} pages, sha256 {res.sha256[:12]})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out")
