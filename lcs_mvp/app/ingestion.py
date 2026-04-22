from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx
from pypdf import PdfReader
from fastapi import HTTPException

from .linting import _normalize_steps


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _pdf_is_scanned(pages: list[dict[str, Any]], threshold_chars_per_page: int = 50) -> bool:
    """Return True if the PDF appears to be scanned (image-only, no extractable text).

    Checks average character count across all pages against a threshold.
    A genuine text PDF will have well over 50 chars/page on average.
    """
    if not pages:
        return True
    total = sum(len((p.get("text") or "").strip()) for p in pages)
    return (total / len(pages)) < threshold_chars_per_page


def _pdf_extract_pages(pdf_path: str) -> list[dict[str, Any]]:
    reader = PdfReader(pdf_path)
    pages: list[dict[str, Any]] = []
    for idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append({"page": idx, "text": text})
    return pages


def _chunk_text(pages: list[dict[str, Any]], max_chars: int = 12000, section_title: str = "") -> list[dict[str, Any]]:
    """Chunk by character count, preserving page numbers."""
    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    buf_pages: list[int] = []
    size = 0

    def flush():
        nonlocal buf, buf_pages, size
        if not buf:
            return
        chunks.append({"pages": sorted(set(buf_pages)), "text": "\n\n".join(buf).strip(), "section_title": section_title})
        buf, buf_pages, size = [], [], 0

    for p in pages:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        header = f"[PAGE {p['page']}]"
        block = header + "\n" + t
        if size + len(block) > max_chars and buf:
            flush()
        buf.append(block)
        buf_pages.append(int(p["page"]))
        size += len(block)

    flush()
    return chunks


def _pdf_extract_outline(pdf_path: str) -> list[dict[str, Any]]:
    """Extract PDF bookmark outline as a flat list of {title, page, level} sorted by page.

    level=0 is top-level (chapter), level=1 is section, etc.
    Returns [] if the PDF has no outline or extraction fails.
    """
    try:
        reader = PdfReader(pdf_path)
        raw = reader.outline
        if not raw:
            return []

        result: list[dict[str, Any]] = []

        def _walk(items: list, depth: int = 0) -> None:
            for item in items:
                if isinstance(item, list):
                    _walk(item, depth + 1)
                else:
                    try:
                        page_num = reader.get_destination_page_number(item) + 1  # 1-based
                        title = (getattr(item, "title", None) or "").strip()
                        if title:
                            result.append({"title": title, "page": page_num, "level": depth})
                    except Exception:
                        pass

        _walk(raw)
        result.sort(key=lambda x: x["page"])
        return result
    except Exception:
        return []


def _chunk_by_structure(
    pages: list[dict[str, Any]],
    outline: list[dict[str, Any]],
    max_chars: int = 15000,
) -> list[dict[str, Any]]:
    """Chunk pages by chapter/section boundaries from the PDF outline.

    Each outline entry defines where a section starts. Pages between two consecutive
    entries belong to the earlier section. Sections that exceed max_chars are further
    split using _chunk_text() at subsection granularity.

    Each chunk carries section_level (0=chapter, 1=section, 2=subsection, …).
    """
    if not outline or not pages:
        return _chunk_text(pages, max_chars)

    # Build a lookup: page_number -> (title, level) — last entry wins per page
    page_to_section: dict[int, tuple[str, int]] = {}
    for entry in outline:
        page_to_section[entry["page"]] = (entry["title"], entry.get("level", 0))

    # Assign each page to a section via the outline boundaries
    section_page_lists: list[tuple[str, int, list[dict[str, Any]]]] = []
    current_title = ""
    current_level = 0
    current_pages: list[dict[str, Any]] = []

    for p in pages:
        pnum = int(p["page"])
        if pnum in page_to_section:
            # Flush previous section
            if current_pages:
                section_page_lists.append((current_title, current_level, current_pages))
            current_title, current_level = page_to_section[pnum]
            current_pages = []
        current_pages.append(p)

    if current_pages:
        section_page_lists.append((current_title, current_level, current_pages))

    # For each section, produce one or more chunks (splitting if too large)
    chunks: list[dict[str, Any]] = []
    for title, level, sec_pages in section_page_lists:
        sub = _chunk_text(sec_pages, max_chars, section_title=title)
        for ch in sub:
            ch["section_level"] = level
        chunks.extend(sub)

    return chunks


# ---------------------------------------------------------------------------
# Generic LLM provider (OpenAI-compatible)
# ---------------------------------------------------------------------------

def _llm_candidate_urls(base_url: str, suffix: str) -> list[str]:
    """Return candidate URLs to try in order for a given path suffix.

    Handles both root base URLs (https://host) and versioned ones
    (https://host/openai/v1) by trying the suffix directly first,
    then prepending /v1/ and /api/v1/ as fallbacks.
    """
    bu = base_url.rstrip("/")
    return [
        f"{bu}/{suffix}",           # base already includes /v1 (e.g. .../openai/v1)
        f"{bu}/v1/{suffix}",        # standard OpenAI root
        f"{bu}/api/v1/{suffix}",    # LM Studio / Ollama legacy
    ]


def _llm_probe(base_url: str, api_key: str = "") -> dict[str, Any]:
    """Health probe for any OpenAI-compatible endpoint.

    Returns {"ok": bool, "detail": str}.
    """
    bu = (base_url or "").rstrip("/")
    if not bu:
        return {"ok": False, "detail": "No LLM base URL configured."}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0), verify=False) as client:
            last_status = None
            for url in _llm_candidate_urls(bu, "models"):
                r = client.get(url, headers=headers)
                if r.status_code < 400:
                    return {"ok": True, "detail": "ok"}
                last_status = r.status_code
            return {"ok": False, "detail": f"HTTP {last_status}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _llm_chat(messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
    """Call any OpenAI-compatible chat completions endpoint.

    cfg is the dict returned by database._get_llm_config().
    Raises HTTPException(504) on timeout, HTTPException(502) on other errors.
    """
    bu = (cfg.get("llm_base_url") or "").rstrip("/")
    if not bu:
        raise HTTPException(status_code=503, detail="LLM not configured. Ask an admin to set up the LLM provider.")

    api_key = cfg.get("llm_api_key") or ""
    model = cfg.get("llm_model") or ""
    timeout_s = float(cfg.get("llm_timeout_seconds") or 120)
    max_tokens = int(cfg.get("llm_max_tokens") or 2000)
    temperature = float(cfg.get("llm_temperature") or 0.2)

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if model:
        payload["model"] = model

    def _extract_content(data: Any) -> str | None:
        if isinstance(data, dict) and "choices" in data:
            return data["choices"][0]["message"]["content"]
        if isinstance(data, dict) and "message" in data and isinstance(data["message"], dict):
            return data["message"].get("content", "")
        return None

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=30.0), verify=False) as client:
            last_err: str = ""
            for url in _llm_candidate_urls(bu, "chat/completions"):
                r = client.post(url, json=payload, headers=headers)
                if r.status_code == 404:
                    last_err = f"HTTP 404 at {url}"
                    continue
                if r.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"LLM API error {r.status_code}: {r.text[:500]}")
                content = _extract_content(r.json())
                if content is not None:
                    return content
                return json.dumps(r.json())
            raise HTTPException(status_code=502, detail=f"No chat/completions endpoint found. Last error: {last_err}")
    except HTTPException:
        raise
    except httpx.ReadTimeout as e:
        raise HTTPException(status_code=504, detail=f"LLM request timed out after {timeout_s}s: {e}")
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"LLM connection error: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"LLM HTTP error: {e}")


# ---------------------------------------------------------------------------
# Fingerprinting and deduplication
# ---------------------------------------------------------------------------

def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _short_code(prefix: str, record_id: str) -> str:
    """Deterministic short display id (for human-visible trace tags)."""
    h = hashlib.sha256((record_id or "").encode("utf-8", errors="ignore")).hexdigest().upper()
    return f"{prefix}-{h[:6]}"


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _task_fingerprint(task: dict[str, Any]) -> str:
    """Deterministic fingerprint for exact-ish dedupe."""
    title = _norm_text(str(task.get("title", "")))
    outcome = _norm_text(str(task.get("outcome", "")))
    steps = task.get("steps") or []
    steps_norm = _normalize_steps(steps)
    parts: list[str] = [title, outcome]
    for st in steps_norm:
        parts.append(_norm_text(str(st.get("text", ""))))
        parts.append(_norm_text(str(st.get("completion", ""))))
    raw = "\n".join(parts).encode("utf-8", errors="ignore")
    return _sha256_bytes(raw)


def _extract_step_targets(steps: list[dict[str, Any]]) -> set[str]:
    """Extract rough targets for near-duplicate hints (paths, services, packages)."""
    targets: set[str] = set()
    path_re = re.compile(r"(/etc/[^\s]+|/var/[^\s]+|/usr/[^\s]+|/opt/[^\s]+)")
    svc_re = re.compile(r"\b(systemctl)\s+(restart|reload|enable|disable)\s+([a-zA-Z0-9_.@-]+)")
    pkg_re = re.compile(r"\bapt(-get)?\s+install\s+(-y\s+)?([a-zA-Z0-9+_.:-]+)")

    for st in steps or []:
        t = (st.get("text") or "") + "\n" + (st.get("completion") or "")
        for m in path_re.findall(t):
            targets.add(m.lower())
        for m in svc_re.findall(t):
            targets.add(f"service:{m[2].lower()}")
        for m in pkg_re.findall(t):
            targets.add(f"pkg:{m[2].lower()}")
    return targets


def _near_duplicate_score(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Heuristic similarity score in [0,1]."""
    a_steps = _normalize_steps(a.get("steps") or [])
    b_steps = _normalize_steps(b.get("steps") or [])

    a_title = set(_norm_text(str(a.get("title", ""))).split())
    b_title = set(_norm_text(str(b.get("title", ""))).split())
    a_out = set(_norm_text(str(a.get("outcome", ""))).split())
    b_out = set(_norm_text(str(b.get("outcome", ""))).split())

    def jacc(x: set[str], y: set[str]) -> float:
        if not x and not y:
            return 0.0
        return len(x & y) / max(1, len(x | y))

    title_sim = jacc(a_title, b_title)
    out_sim = jacc(a_out, b_out)

    a_tgt = _extract_step_targets(a_steps)
    b_tgt = _extract_step_targets(b_steps)
    tgt_sim = jacc(a_tgt, b_tgt)

    # Weighted: outcome + targets matter more than title.
    return 0.20 * title_sim + 0.45 * out_sim + 0.35 * tgt_sim


# ---------------------------------------------------------------------------
# Triage and schema 1.0 extraction
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """Classify this section of technical documentation as exactly one of:
- "task": describes one or more concrete procedures an operator would perform
- "workflow": describes a sequence of multiple distinct procedures achieving a larger outcome
- "ignore": administrative, introductory, legal, appendix, glossary, index, or no actionable procedural content

Return JSON only — no markdown, no commentary:
{"type": "task", "confidence": 0.0, "reason": "one sentence"}"""

_EXTRACT_TASK_SYSTEM = """You are generating a Blueprinted JSON import payload from a section of technical documentation.
Follow these rules exactly:

1. Output valid JSON only. No preamble, no explanation, no markdown code fences.
2. Assign each task a sequential ID starting at T001, incrementing by one (T001, T002, T003...). Do not skip, reuse, or modify IDs.
3. Every field defined in the schema must be present in every object, even if the value is null, false, or an empty array. Never omit a field.
4. For every step, write the completion condition first. If the completion condition is a restatement of the action, discard the step and merge its action into the preceding step. Only keep steps where the completion condition describes an observable, independently verifiable state change.
5. Steps must be atomic — one operation per step. No compound instructions. Imperative form only.
6. The irreversible field must always be present and must be a boolean (true or false).
7. facts, concepts, dependencies, and actions may be empty arrays ([]) but must always be present.
8. Do not invent content. Only extract what is present in the source text.

Return JSON with this exact structure:
{"tasks": [{"id": "T001", "title": "...", "outcome": "...", "procedure_name": "...", "facts": [], "concepts": [], "dependencies": [], "irreversible": false, "steps": [{"text": "...", "completion": "...", "actions": []}]}], "workflows": []}"""

_EXTRACT_WORKFLOW_SYSTEM = """You are generating a Blueprinted JSON import payload from a section of technical documentation.
Follow these rules exactly:

1. Output valid JSON only. No preamble, no explanation, no markdown code fences.
2. Assign each task a sequential ID starting at T001, incrementing by one (T001, T002, T003...). Do not skip, reuse, or modify IDs.
3. Every field defined in the schema must be present in every object, even if the value is null, false, or an empty array. Never omit a field.
4. For every step, write the completion condition first. If the completion condition is a restatement of the action, discard the step and merge its action into the preceding step. Only keep steps where the completion condition describes an observable, independently verifiable state change.
5. Steps must be atomic — one operation per step. No compound instructions. Imperative form only.
6. The irreversible field must always be present and must be a boolean (true or false).
7. facts, concepts, dependencies, and actions may be empty arrays ([]) but must always be present.
8. Do not invent content. Only extract what is present in the source text.
9. When referencing tasks in workflow task_order, copy the task ID exactly as assigned (e.g. "T001"). Do not use task titles in task_order.
10. Propose a workflow only if the section explicitly describes a sequence or grouping of multiple distinct tasks.

Return JSON with this exact structure:
{"tasks": [{"id": "T001", "title": "...", "outcome": "...", "procedure_name": "...", "facts": [], "concepts": [], "dependencies": [], "irreversible": false, "steps": [{"text": "...", "completion": "...", "actions": []}]}], "workflows": [{"title": "...", "objective": "...", "task_order": ["T001", "T002"]}]}"""


def _llm_triage_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Classify a chunk as task/workflow/ignore. Skips LLM for sparse chunks."""
    stripped = (text or "").strip()
    if len(stripped) < 100:
        return {"type": "ignore", "confidence": 1.0, "reason": "sparse section"}

    user_msg = f"SECTION: {section_title}\n\nTEXT:\n{stripped[:6000]}"
    triage_cfg = dict(cfg)
    triage_cfg["max_tokens"] = 80
    triage_cfg["temperature"] = 0.0
    try:
        raw = _llm_chat([
            {"role": "system", "content": _TRIAGE_SYSTEM},
            {"role": "user", "content": user_msg},
        ], triage_cfg)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        chunk_type = str(result.get("type", "ignore")).lower()
        if chunk_type not in ("task", "workflow", "ignore"):
            chunk_type = "ignore"
        return {
            "type": chunk_type,
            "confidence": float(result.get("confidence", 0.5)),
            "reason": str(result.get("reason", ""))[:300],
        }
    except Exception:
        return {"type": "task", "confidence": 0.3, "reason": "classification failed — defaulting to task"}


def _llm_extract_task_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract schema 1.0 task fragment from a task-type chunk."""
    user_msg = f"SECTION: {section_title}\n\nSOURCE TEXT:\n{(text or '').strip()}"
    raw = _llm_chat([
        {"role": "system", "content": _EXTRACT_TASK_SYSTEM},
        {"role": "user", "content": user_msg},
    ], cfg)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    result = json.loads(raw)
    if not isinstance(result.get("tasks"), list):
        result["tasks"] = []
    if not isinstance(result.get("workflows"), list):
        result["workflows"] = []
    return result


def _llm_extract_workflow_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract schema 1.0 task+workflow fragment from a workflow-type chunk."""
    user_msg = f"SECTION: {section_title}\n\nSOURCE TEXT:\n{(text or '').strip()}"
    raw = _llm_chat([
        {"role": "system", "content": _EXTRACT_WORKFLOW_SYSTEM},
        {"role": "user", "content": user_msg},
    ], cfg)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    result = json.loads(raw)
    if not isinstance(result.get("tasks"), list):
        result["tasks"] = []
    if not isinstance(result.get("workflows"), list):
        result["workflows"] = []
    return result
