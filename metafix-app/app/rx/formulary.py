"""Formulary lookup: resolve a prescribed line to brand, molecules and strength.

"Tab. Xilingio 25/5"  -> brand Xilingio, strength "25/5", molecules [empagliflozin, linagliptin]
"Levothyroxine 7.5 mcg" -> generic levothyroxine, strength "7.5"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

FORMULARY_PATH = Path(__file__).resolve().parent / "validate" / "formulary.yaml"

FORM_PREFIX = re.compile(
    r"^\s*(tab|tabs|tablet|cap|caps|capsule|inj|injection|syp|syrup|sachet|susp|drops?|oint|gel|cream|pen|spray|neb)\.?\s+",
    re.I,
)
NUM = r"\d+(?:\.\d+)?"
STRENGTH_RE = re.compile(rf"({NUM}(?:\s*(?:mg|mcg|µg|g|iu|units?|ml|%))?(?:\s*/\s*{NUM}(?:\s*(?:mg|mcg|µg|g|iu|ml))?)*)", re.I)


@dataclass
class Resolved:
    raw: str
    brand: Optional[str] = None
    generic: Optional[str] = None  # molecule when prescribed by generic name
    molecules: list[str] = field(default_factory=list)
    strength: Optional[str] = None
    known_strengths: list[str] = field(default_factory=list)
    schedule: Optional[str] = None
    classes: set[str] = field(default_factory=set)

    @property
    def in_formulary(self) -> bool:
        return bool(self.brand or self.generic)


@lru_cache
def formulary() -> dict:
    with open(FORMULARY_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data["molecules"] = {k.lower(): v for k, v in (data.get("molecules") or {}).items()}
    data["brands"] = data.get("brands") or {}
    # longest names first so "Montair LC Kid" wins over "Montair LC" over "Montair"
    data["_brand_order"] = sorted(data["brands"], key=len, reverse=True)
    data["_mol_order"] = sorted(data["molecules"], key=len, reverse=True)
    return data


def strength_key(s: str | None) -> Optional[tuple[float, ...]]:
    if not s:
        return None
    nums = re.findall(NUM, s)
    return tuple(float(n) for n in nums) if nums else None


def _starts_with_word(text: str, name: str) -> bool:
    return re.match(rf"{re.escape(name)}(?![\w-])", text, re.I) is not None


def resolve(name: str, generic: str | None = None) -> Resolved:
    f = formulary()
    text = FORM_PREFIX.sub("", name or "").strip()
    res = Resolved(raw=name)
    for b in f["_brand_order"]:
        if _starts_with_word(text, b):
            info = f["brands"][b]
            res.brand = b
            res.molecules = [m.lower() for m in info.get("molecules", [])]
            res.known_strengths = [str(s) for s in info.get("strengths") or []]
            res.schedule = info.get("schedule")
            rest = text[len(b):].strip()
            m = STRENGTH_RE.search(rest)
            res.strength = m.group(1).strip() if m else None
            break
    else:
        for mol in f["_mol_order"]:
            if _starts_with_word(text, mol):
                info = f["molecules"][mol]
                res.generic = mol
                res.molecules = [mol]
                res.known_strengths = [str(s) for s in info.get("strengths") or []]
                res.schedule = info.get("schedule")
                m = STRENGTH_RE.search(text[len(mol):])
                res.strength = m.group(1).strip() if m else None
                break
    # a generic line ("empagliflozin + linagliptin") adds molecules even for unknown brands
    for mol in molecules_in_text(generic or ""):
        if mol not in res.molecules:
            res.molecules.append(mol)
    if not res.molecules:
        res.molecules = molecules_in_text(text)
    for mol in res.molecules:
        cls = (f["molecules"].get(mol) or {}).get("class")
        if cls:
            res.classes.add(cls)
        if res.schedule is None and len(res.molecules) == 1:
            res.schedule = (f["molecules"].get(mol) or {}).get("schedule")
    return res


def molecules_in_text(text: str) -> list[str]:
    f = formulary()
    low = (text or "").lower()
    found: list[str] = []
    for mol in f["_mol_order"]:
        if re.search(rf"(?<![\w-]){re.escape(mol)}(?![\w-])", low) and not any(mol in x for x in found):
            found.append(mol)
    return found


def strength_ok(res: Resolved) -> Optional[bool]:
    """True/False when checkable, None when there is nothing to check."""
    if not res.in_formulary or not res.strength or not res.known_strengths:
        return None
    key = strength_key(res.strength)
    known = {strength_key(s) for s in res.known_strengths}
    return key in known
