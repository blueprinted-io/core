"""Structured field-level diffing for task, primer and workflow revisions."""
from __future__ import annotations
import difflib
import html
import json
from typing import Any


def _word_diff_html(old: str, new: str) -> str:
    """Return an HTML string with word-level additions/deletions marked up."""
    old_words = old.split()
    new_words = new.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    parts: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.append(html.escape(" ".join(old_words[i1:i2])))
        elif op == "replace":
            parts.append(f'<del class="diff-del">{html.escape(" ".join(old_words[i1:i2]))}</del>')
            parts.append(f'<ins class="diff-ins">{html.escape(" ".join(new_words[j1:j2]))}</ins>')
        elif op == "delete":
            parts.append(f'<del class="diff-del">{html.escape(" ".join(old_words[i1:i2]))}</del>')
        elif op == "insert":
            parts.append(f'<ins class="diff-ins">{html.escape(" ".join(new_words[j1:j2]))}</ins>')
    return " ".join(parts)


def _text_field(label: str, old: str, new: str) -> dict | None:
    old = (old or "").strip()
    new = (new or "").strip()
    if old == new:
        return None
    return {
        "label": label,
        "type": "text",
        "old": old,
        "new": new,
        "diff_html": _word_diff_html(old, new),
    }


def _list_field(label: str, old_list: list[str], new_list: list[str]) -> dict | None:
    if old_list == new_list:
        return None
    old_set = set(old_list)
    new_set = set(new_list)
    added = [x for x in new_list if x not in old_set]
    removed = [x for x in old_list if x not in new_set]
    if not added and not removed:
        return None
    return {
        "label": label,
        "type": "list",
        "added": added,
        "removed": removed,
    }


def _load(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def diff_task(old: dict, new: dict) -> list[dict]:
    fields: list[dict] = []

    for label, key in [
        ("Title", "title"),
        ("Outcome", "outcome"),
        ("Procedure name", "procedure_name"),
        ("Domain", "domain"),
        ("Software name", "software_name"),
        ("Software version", "software_version"),
    ]:
        f = _text_field(label, old.get(key), new.get(key))
        if f:
            fields.append(f)

    old_irrev = bool(old.get("irreversible_flag"))
    new_irrev = bool(new.get("irreversible_flag"))
    if old_irrev != new_irrev:
        fields.append({
            "label": "Irreversible",
            "type": "bool",
            "old": old_irrev,
            "new": new_irrev,
        })

    for label, key in [
        ("Facts", "facts_json"),
        ("Concepts", "concepts_json"),
        ("Dependencies", "dependencies_json"),
    ]:
        f = _list_field(label, _load(old.get(key)), _load(new.get(key)))
        if f:
            fields.append(f)

    old_steps = _load(old.get("steps_json"))
    new_steps = _load(new.get("steps_json"))
    if old_steps != new_steps:
        step_diffs: list[dict] = []
        for i in range(max(len(old_steps), len(new_steps))):
            o = old_steps[i] if i < len(old_steps) else None
            n = new_steps[i] if i < len(new_steps) else None
            if o is None:
                step_diffs.append({"index": i + 1, "change": "added", "step": n})
            elif n is None:
                step_diffs.append({"index": i + 1, "change": "removed", "step": o})
            elif o != n:
                text_diff_html = (
                    _word_diff_html(o.get("text", ""), n.get("text", ""))
                    if o.get("text") != n.get("text") else None
                )
                step_diffs.append({
                    "index": i + 1,
                    "change": "modified",
                    "old": o,
                    "new": n,
                    "text_diff_html": text_diff_html,
                })
        if step_diffs:
            fields.append({"label": "Steps", "type": "steps", "diffs": step_diffs})

    return fields


def diff_primer(old: dict, new: dict) -> list[dict]:
    fields: list[dict] = []
    for label, key in [
        ("Title", "title"),
        ("Domain", "domain"),
        ("Summary", "summary"),
        ("Explanation", "explanation"),
        ("Analogies", "analogies"),
    ]:
        f = _text_field(label, old.get(key), new.get(key))
        if f:
            fields.append(f)
    return fields


def diff_workflow(
    old: dict, new: dict,
    old_task_refs: list[dict], new_task_refs: list[dict],
    old_primer_ids: list[str], new_primer_ids: list[str],
) -> list[dict]:
    fields: list[dict] = []

    for label, key in [("Title", "title"), ("Objective", "objective")]:
        f = _text_field(label, old.get(key), new.get(key))
        if f:
            fields.append(f)

    # Task refs
    old_keys = [(r["record_id"], int(r["version"])) for r in old_task_refs]
    new_keys = [(r["record_id"], int(r["version"])) for r in new_task_refs]
    if old_keys != new_keys:
        old_by_rid = {r["record_id"]: r for r in old_task_refs}
        new_by_rid = {r["record_id"]: r for r in new_task_refs}
        old_rid_set = set(old_by_rid)
        new_rid_set = set(new_by_rid)

        added = [new_by_rid[rid] for rid in new_rid_set - old_rid_set]
        removed = [old_by_rid[rid] for rid in old_rid_set - new_rid_set]
        updated = [
            {"old": old_by_rid[rid], "new": new_by_rid[rid]}
            for rid in old_rid_set & new_rid_set
            if int(old_by_rid[rid]["version"]) != int(new_by_rid[rid]["version"])
        ]
        if added or removed or updated:
            fields.append({
                "label": "Tasks",
                "type": "task_refs",
                "added": added,
                "removed": removed,
                "updated": updated,
            })

    # Primer refs
    old_p = set(old_primer_ids)
    new_p = set(new_primer_ids)
    if old_p != new_p:
        fields.append({
            "label": "Primers",
            "type": "primer_ids",
            "added": sorted(new_p - old_p),
            "removed": sorted(old_p - new_p),
        })

    return fields
