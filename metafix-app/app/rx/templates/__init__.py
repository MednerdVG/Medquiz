"""Template library loader (brief §12).

Templates are YAML files under diet/, exercise/, advice/, charts/, certificates/.
Each carries `id`, `version` and a body. The version used is recorded in the
archived prescription JSON (`template_versions`).
"""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

import yaml

TEMPLATES_DIR = Path(__file__).resolve().parent
KINDS = ("diet", "exercise", "advice", "charts", "certificates")

# Aliases so documents written with older ids keep rendering.
ALIASES = {
    ("advice", "sick_day"): "sick_day_rules",
    ("charts", "sugar_log_4col"): "sugar_log",
}


class TemplateNotFound(KeyError):
    pass


@lru_cache
def _index(kind: str) -> dict[str, dict]:
    if kind not in KINDS:
        raise ValueError(f"unknown template kind {kind!r}")
    out = {}
    for path in sorted((TEMPLATES_DIR / kind).glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        tid = data.get("id") or path.stem
        data["id"] = tid
        data.setdefault("version", "1")
        out[tid] = data
    return out


def list_templates(kind: str) -> list[dict]:
    return [
        {"id": t["id"], "version": str(t["version"]), "title": t.get("title", t["id"]),
         "summary": t.get("summary", "")}
        for t in _index(kind).values()
    ]


def resolve_id(kind: str, tid: str) -> str:
    return ALIASES.get((kind, tid), tid)


def load(kind: str, tid: str) -> dict:
    tid = resolve_id(kind, tid)
    try:
        return copy.deepcopy(_index(kind)[tid])
    except KeyError as exc:
        raise TemplateNotFound(f"{kind} template '{tid}' does not exist") from exc


def exists(kind: str, tid: str) -> bool:
    return resolve_id(kind, tid) in _index(kind)


def deep_merge(base, override):
    """Overrides replace lists wholesale and merge dicts recursively.

    Slot-style lists of dicts with a `key` can be patched by key when the override
    is a dict: {"slots": {"breakfast": {"options": [...]}}}.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(override, dict) and all(isinstance(i, dict) and "key" in i for i in base):
        out = []
        for item in base:
            patch = override.get(item["key"])
            if patch is None:
                out.append(item)
            elif patch == "__remove__":
                continue
            else:
                out.append(deep_merge(item, patch))
        return out
    return copy.deepcopy(override)


def load_with_overrides(kind: str, tid: str, overrides: dict | None) -> dict:
    base = load(kind, tid)
    return deep_merge(base, overrides) if overrides else base
