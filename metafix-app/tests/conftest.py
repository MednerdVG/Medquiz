import copy
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="session", autouse=True)
def specimen_signatures(tmp_path_factory):
    """Transparent PNGs labelled SPECIMEN stand in for the clinic's real signature scans."""
    d = tmp_path_factory.mktemp("signatures")
    from app.config import doctors
    from app.rx.render.renderer import _Browser
    with _Browser() as br:
        for doc in doctors().values():
            if doc.sign_style != "image":
                continue
            br.page.set_content(
                "<html><body style='margin:0;background:transparent'>"
                f"<div id=s style='display:inline-block;font:italic 28px serif;color:#123;padding:4px'>"
                f"SPECIMEN {doc.key}</div></body></html>")
            br.page.locator("#s").screenshot(path=str(d / Path(doc.signature_png).name), omit_background=True)
    os.environ["METAFIX_SIGNATURES_DIR"] = str(d)
    yield d


@pytest.fixture
def first_visit_json():
    return json.loads((EXAMPLES / "first_visit.json").read_text(encoding="utf-8"))


@pytest.fixture
def acceptance_json():
    return json.loads((EXAMPLES / "acceptance_first_visit.json").read_text(encoding="utf-8"))


@pytest.fixture
def rx_factory(first_visit_json):
    from app.rx.schema import Prescription

    def make(**patch):
        data = copy.deepcopy(first_visit_json)
        for dotted, value in patch.items():
            cur = data
            keys = dotted.split("__")
            for k in keys[:-1]:
                cur = cur[k]
            cur[keys[-1]] = value
        return Prescription.model_validate(data)
    return make


def pdf_text(pdf: bytes) -> list[str]:
    import io
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages]


def squash(text: str) -> str:
    """Whitespace-free text: letter-spaced headings extract as 'C E RT I F I C AT E'."""
    import re
    return re.sub(r"\s+", "", text)
