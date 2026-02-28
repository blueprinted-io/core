from __future__ import annotations

import json
from typing import Any


def _json_load(s: str) -> Any:
    return json.loads(s) if s else None


def _json_dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def parse_lines(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return [ln for ln in lines if ln]


def parse_tags(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def parse_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for ln in parse_lines(text or ""):
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        meta[k] = v
    return meta
