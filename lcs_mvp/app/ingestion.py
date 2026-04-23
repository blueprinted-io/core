from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx
from pypdf import PdfReader
from fastapi import HTTPException

from .linting import _normalize_steps

logger = logging.getLogger("blueprinted.ingestion")


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

    def _extract_content(data: Any) -> tuple[str | None, str | None]:
        """Returns (content, finish_reason). content is None if not extractable."""
        if isinstance(data, dict) and "choices" in data:
            choice = data["choices"][0]
            msg = choice.get("message", {})
            finish = choice.get("finish_reason")
            content = msg.get("content")
            return content, finish
        if isinstance(data, dict) and "message" in data and isinstance(data["message"], dict):
            return data["message"].get("content"), None
        return None, None

    logger.debug("LLM request model=%s max_tokens=%s url=%s", model or "(default)", max_tokens, bu)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=30.0), verify=False) as client:
            last_err: str = ""
            for url in _llm_candidate_urls(bu, "chat/completions"):
                r = client.post(url, json=payload, headers=headers)
                if r.status_code == 404:
                    last_err = f"HTTP 404 at {url}"
                    continue
                if r.status_code >= 400:
                    err = f"LLM API error {r.status_code}: {r.text[:500]}"
                    logger.error("LLM API error: %s", err)
                    raise HTTPException(status_code=502, detail=err)
                data = r.json()
                content, finish_reason = _extract_content(data)
                usage = data.get("usage", {})
                logger.debug(
                    "LLM response finish_reason=%s prompt_tokens=%s completion_tokens=%s",
                    finish_reason, usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                )
                if finish_reason == "length":
                    msg = (
                        f"LLM hit max_tokens limit (finish_reason=length) before producing output. "
                        f"Increase max_tokens in admin LLM settings (current: {max_tokens}). "
                        f"Reasoning models like GLM-4.7 need 8000+ tokens."
                    )
                    logger.error("LLM max_tokens exhausted: model=%s max_tokens=%s", model, max_tokens)
                    raise HTTPException(status_code=502, detail=msg)
                if content is not None:
                    return content
                err = f"LLM response at {url} had no extractable content field. Response: {r.text[:300]}"
                logger.error("LLM no content: %s", err)
                raise HTTPException(status_code=502, detail=err)
            raise HTTPException(status_code=502, detail=f"No chat/completions endpoint found. Last error: {last_err}")
    except HTTPException:
        raise
    except httpx.ReadTimeout as e:
        logger.error("LLM timeout after %ss: %s", timeout_s, e)
        raise HTTPException(status_code=504, detail=f"LLM request timed out after {timeout_s}s: {e}")
    except httpx.ConnectError as e:
        logger.error("LLM connection error: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM connection error: {e}")
    except httpx.HTTPError as e:
        logger.error("LLM HTTP error: %s", e)
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

_EXTRACT_TASK_SYSTEM = """You are extracting structured task records from a section of technical documentation.

## Field definitions

outcome: A single sentence in passive voice describing the observable end state after all steps are complete. Specific to this procedure.

facts: Background knowledge the learner needs about the subject matter before they can make sense of this task. The "what" — what are the components involved, what do they do, what are they for. This is not technical reference data (commands and port numbers belong in steps); it is the definitional understanding a learner needs so they are not confused about what they are working with. Can be short for simple tasks, long for complex ones. Write each as a complete sentence.
  Good: "Veeam Agent for Microsoft Windows is a backup agent installed locally on each Windows machine that Veeam will protect." / "iscsid is the iSCSI daemon that manages active iSCSI sessions on the local machine." / "open-iscsi is the Linux iSCSI initiator stack — the complete set of kernel modules and userspace tools that allow a Linux machine to connect to iSCSI targets."
  Bad: "The default iSCSI port is 3260." (technical trivia, not definitional knowledge) / "Run sudo apt install open-iscsi." (belongs in steps)

concepts: The core reason why this task must be done — the essential principle or necessity that makes this task exist. For atomic tasks like installing software, the concept is why the software needs to be installed at all, not how the installation works internally. For configuration tasks, it is what breaks without that configuration. Stay focused on the core principle; implementation details and "by the way" information belong in step notes, not here. Write in plain English. A substantive explanation of one or two paragraphs is expected; do not summarise to a single line.
  Good: a paragraph explaining that Veeam's architecture requires a locally-running agent on each protected Windows machine — without it, the Veeam server has no mechanism to interact with that machine's storage or OS-level APIs and cannot perform any backup operations.
  Bad: "The installation process involves extracting files and registering a service; some components may require a reboot." (installation mechanics — belongs in step notes, not concepts)

dependencies: Specific preconditions that must be true before the operator can start. Full sentences.
  Good: "Ubuntu machine is accessible with sudo privileges." / "No backup jobs are currently running."

procedure_name: A short imperative phrase naming the method used, distinct from the task title.
  Example: title "Upgrade Veeam Agent for Microsoft Windows" → procedure_name "Interactive upgrade via Control Panel"

irreversible: true only if completing this task produces changes that are difficult or impossible to undo without significant additional work or data loss risk. Formatting a disk = true. Installing or upgrading software = false.

steps: Each step is a single physical or digital action.
  - Start with a concrete verb: open, close, press, click, run, record, insert, remove, verify, enter, select.
  - Do NOT start with abstract verbs: configure, manage, set up, ensure, handle, prepare, edit.
  - One action only. If the step contains "and", "then", or "also", split it into two consecutive steps.
  - text: the instruction itself.
  - completion: observable confirmation the step is done. Specific — not "Step is complete." or "Done."
      Good: "Terminal shows 'OK'." / "Wizard advances to the License Agreement screen."
      Bad: "Software is installed." / "Step is complete."
  - actions: array of substeps giving the concrete method — menu navigation paths, exact CLI commands, keyboard shortcuts. Empty array [] if the step text is self-explanatory.
  - notes: "oh by the way" information from the source — edge cases, uncommon configurations, or conditional caveats that don't always apply. Extract from callouts, notes, or asides in the source text. null if none.

## Output rules

1. Output valid JSON only. No preamble, no explanation, no markdown code fences.
2. Assign each task a sequential ID starting at T001 (T001, T002, T003...). Never skip or reuse IDs.
3. Every field must be present in every object, even if null, false, or []. Never omit a field.
4. Do not invent content. Extract only what is present in the source text. If facts, concepts, or dependencies are not present in the source, use [].

## Example

{"tasks":[{"id":"T001","title":"Install the iSCSI initiator utilities","outcome":"The open-iscsi package is installed and the iscsid service is running on the Ubuntu machine.","procedure_name":"Install open-iscsi via apt","facts":["open-iscsi is the Linux iSCSI initiator stack — the complete set of kernel modules and userspace tools that allow a Linux machine to discover, connect to, and maintain sessions with iSCSI targets.","iscsid is the iSCSI daemon process; it runs in the background and manages all active iSCSI sessions on the local machine.","iscsiadm is the command-line management interface for iSCSI on Linux; it is installed as part of open-iscsi and is used for all subsequent iSCSI configuration and discovery operations."],"concepts":["iSCSI works by running a protocol stack on the machine that needs remote storage (the initiator) to communicate with a server that exposes that storage (the target). open-iscsi is the entire initiator stack for Linux. Until it is installed and running, the Linux machine has no mechanism to speak the iSCSI protocol at all — there is no driver to make connections, no daemon to manage sessions, and no tooling to configure targets. Installing open-iscsi is therefore the first mandatory step before any iSCSI storage can be used on a Linux machine."],"dependencies":["Ubuntu machine is accessible with sudo privileges.","Machine has internet or local repository access."],"irreversible":false,"steps":[{"text":"Update the package index.","completion":"Completes without error.","actions":["sudo apt update"],"notes":null},{"text":"Install the open-iscsi package.","completion":"Completes without error, confirming open-iscsi and iscsiadm are installed.","actions":["sudo apt install open-iscsi"],"notes":"If open-iscsi is already installed, apt will report 'open-iscsi is already the newest version' and no further action is required."},{"text":"Enable the iscsid service to start on boot.","completion":"Returns a symlink confirmation line.","actions":["sudo systemctl enable iscsid"],"notes":"On some Ubuntu versions, open-iscsi enables iscsid automatically on installation — if so, this command returns without output and no further action is needed."},{"text":"Start the iscsid service.","completion":"Returns to prompt without error.","actions":["sudo systemctl start iscsid"],"notes":null},{"text":"Confirm the service is active.","completion":"Output shows Active: active (running).","actions":["sudo systemctl status iscsid"],"notes":"On some minimal Ubuntu installations the service may show as 'inactive (dead)' immediately after install — if so, repeat the start command and check again."}]}],"workflows":[]}"""

_EXTRACT_WORKFLOW_SYSTEM = """You are extracting structured task records from a section of technical documentation. This section describes a workflow — a sequence of multiple distinct tasks achieving a larger outcome.

## Field definitions

outcome: A single sentence in passive voice describing the observable end state after all steps are complete. Specific to this procedure.

facts: Background knowledge the learner needs about the subject matter before they can make sense of this task. The "what" — what are the components involved, what do they do, what are they for. This is not technical reference data (commands and port numbers belong in steps); it is the definitional understanding a learner needs so they are not confused about what they are working with. Can be short for simple tasks, long for complex ones. Write each as a complete sentence.
  Good: "Veeam Agent for Microsoft Windows is a backup agent installed locally on each Windows machine that Veeam will protect." / "iscsid is the iSCSI daemon that manages active iSCSI sessions on the local machine."
  Bad: "The default iSCSI port is 3260." (technical trivia, not definitional knowledge) / "Run sudo apt install open-iscsi." (belongs in steps)

concepts: The core reason why this task must be done — the essential principle or necessity that makes this task exist. For atomic tasks like installing software, the concept is why the software needs to be installed at all, not how the installation works internally. For configuration tasks, it is what breaks without that configuration. Stay focused on the core principle; implementation details and "by the way" information belong in step notes, not here. Write in plain English. A substantive explanation of one or two paragraphs is expected; do not summarise to a single line.

dependencies: Specific preconditions that must be true before the operator can start. Full sentences.

procedure_name: A short imperative phrase naming the method used, distinct from the task title.

irreversible: true only if completing this task produces changes that are difficult or impossible to undo without significant additional work or data loss risk.

steps: Each step is a single physical or digital action.
  - Start with a concrete verb: open, close, press, click, run, record, insert, remove, verify, enter, select.
  - Do NOT start with abstract verbs: configure, manage, set up, ensure, handle, prepare, edit.
  - One action only. If the step contains "and", "then", or "also", split it into two consecutive steps.
  - text: the instruction itself.
  - completion: observable confirmation the step is done. Specific — not "Step is complete." or "Done."
  - actions: array of substeps — menu paths, CLI commands, keyboard shortcuts. [] if self-explanatory.
  - notes: edge cases or conditional caveats from the source text. null if none.

workflow objective: The measurable end state the whole sequence achieves. Distinct from any single task outcome — describes what has been accomplished when all tasks are done.

## Output rules

1. Output valid JSON only. No preamble, no explanation, no markdown code fences.
2. Assign each task a sequential ID starting at T001 (T001, T002, T003...). Never skip or reuse IDs.
3. Every field must be present in every object, even if null, false, or []. Never omit a field.
4. Do not invent content. Extract only what is present in the source text.
5. In workflow task_order, copy task IDs exactly as assigned in this response (e.g. "T001"). Do not use task titles.
6. Only propose a workflow if the section explicitly describes a sequence or grouping of multiple distinct tasks.

Return JSON with this structure: {"tasks":[{"id":"T001","title":"...","outcome":"...","procedure_name":"...","facts":[],"concepts":[],"dependencies":[],"irreversible":false,"steps":[{"text":"...","completion":"...","actions":[],"notes":null}]}],"workflows":[{"title":"...","objective":"...","task_order":["T001","T002"]}]}"""


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
        logger.debug("Triage '%s' → %s (confidence=%.2f)", section_title[:60], chunk_type, float(result.get("confidence", 0.5)))
        return {
            "type": chunk_type,
            "confidence": float(result.get("confidence", 0.5)),
            "reason": str(result.get("reason", ""))[:300],
        }
    except Exception as exc:
        logger.warning("Triage failed for '%s': %s — defaulting to task", section_title[:60], exc)
        return {"type": "task", "confidence": 0.3, "reason": "classification failed — defaulting to task"}


def _parse_llm_json(raw: str, section_title: str, max_tokens: int) -> dict[str, Any]:
    """Strip code fences, parse JSON, and raise HTTPException with a helpful message on failure."""
    from json_repair import repair_json

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # Log context around the error position so it's diagnosable in the admin log viewer
        start = max(0, exc.pos - 80)
        end = min(len(raw), exc.pos + 80)
        snippet = repr(raw[start:end])
        logger.warning(
            "JSON parse failed for '%s' at char %d — attempting repair. Context: ...%s...",
            section_title[:80], exc.pos, snippet,
        )
        try:
            repaired = repair_json(raw, return_objects=True)
            if isinstance(repaired, dict):
                logger.info("JSON repair succeeded for '%s'", section_title[:80])
                return repaired
            logger.error(
                "JSON repair returned unexpected type %s for '%s'", type(repaired).__name__, section_title[:80]
            )
        except Exception as repair_exc:
            logger.error("JSON repair also failed for '%s': %s", section_title[:80], repair_exc)

        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM returned malformed JSON for '{section_title[:60]}' (error at char {exc.pos}). "
                f"This is usually caused by unescaped characters in the output or the response being "
                f"cut off before the JSON was complete. Check Admin → App Logs for the raw context. "
                f"If this is a token limit issue, current max_tokens is {max_tokens}."
            ),
        )


def _llm_extract_task_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract schema 1.0 task fragment from a task-type chunk."""
    logger.info("Extracting tasks from '%s'", section_title[:80])
    user_msg = f"SECTION: {section_title}\n\nSOURCE TEXT:\n{(text or '').strip()}"
    raw = _llm_chat([
        {"role": "system", "content": _EXTRACT_TASK_SYSTEM},
        {"role": "user", "content": user_msg},
    ], cfg)
    result = _parse_llm_json(raw, section_title, int(cfg.get("llm_max_tokens") or 2000))
    if not isinstance(result.get("tasks"), list):
        result["tasks"] = []
    if not isinstance(result.get("workflows"), list):
        result["workflows"] = []
    logger.info("Extracted %d task(s) from '%s'", len(result["tasks"]), section_title[:80])
    return result


def _llm_extract_workflow_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract schema 1.0 task+workflow fragment from a workflow-type chunk."""
    logger.info("Extracting workflow from '%s'", section_title[:80])
    user_msg = f"SECTION: {section_title}\n\nSOURCE TEXT:\n{(text or '').strip()}"
    raw = _llm_chat([
        {"role": "system", "content": _EXTRACT_WORKFLOW_SYSTEM},
        {"role": "user", "content": user_msg},
    ], cfg)
    result = _parse_llm_json(raw, section_title, int(cfg.get("llm_max_tokens") or 2000))
    if not isinstance(result.get("tasks"), list):
        result["tasks"] = []
    if not isinstance(result.get("workflows"), list):
        result["workflows"] = []
    logger.info("Extracted %d task(s) + %d workflow(s) from '%s'", len(result["tasks"]), len(result["workflows"]), section_title[:80])
    return result
