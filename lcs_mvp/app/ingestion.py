from __future__ import annotations

import hashlib
import json
import logging
import os
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
# PDF image extraction
# ---------------------------------------------------------------------------

_IMG_MIN_BYTES = 5_000   # skip tiny icons
_IMG_MIN_PX = 80         # skip narrow/short decorative images

def _extract_pdf_images(pdf_path: str, pages: list[int], task_record_id: str) -> list[dict[str, Any]]:
    """Extract qualifying embedded images from the given PDF page numbers.

    Images smaller than _IMG_MIN_BYTES or narrower/shorter than _IMG_MIN_PX
    are discarded. Identical images (same SHA-256) are stored once.
    Returns a list of asset dicts to append to task_assets_json.
    """
    try:
        import fitz  # PyMuPDF
        from .config import TASK_IMAGES_DIR
    except ImportError:
        logger.warning("PyMuPDF not installed — skipping image extraction for task %s", task_record_id[:8])
        return []

    assets: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    out_dir = os.path.join(TASK_IMAGES_DIR, task_record_id)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("Could not open PDF for image extraction (%s): %s", pdf_path, exc)
        return []

    try:
        for page_num in pages:
            page_idx = page_num - 1  # fitz is 0-indexed
            if page_idx < 0 or page_idx >= len(doc):
                continue
            page = doc[page_idx]
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    img = doc.extract_image(xref)
                except Exception:
                    continue

                img_bytes = img.get("image") or b""
                if len(img_bytes) < _IMG_MIN_BYTES:
                    continue
                width = img.get("width", 0)
                height = img.get("height", 0)
                if width < _IMG_MIN_PX or height < _IMG_MIN_PX:
                    continue

                digest = hashlib.sha256(img_bytes).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)

                ext = img.get("ext", "png")
                filename = f"{digest[:16]}.{ext}"

                os.makedirs(out_dir, exist_ok=True)
                img_path = os.path.join(out_dir, filename)
                if not os.path.exists(img_path):
                    with open(img_path, "wb") as f:
                        f.write(img_bytes)

                assets.append({
                    "url": f"/task-images/{task_record_id}/{filename}",
                    "type": "image",
                    "label": f"page {page_num}",
                })
    except Exception as exc:
        logger.warning("Image extraction failed for task %s: %s", task_record_id[:8], exc)
    finally:
        doc.close()

    if assets:
        logger.info("Extracted %d image(s) for task %s", len(assets), task_record_id[:8])
    return assets


def _extract_and_match_images(
    pdf_path: str,
    pages: list[int],
    steps: list[dict[str, Any]],
    task_record_id: str,
    section_ranges: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract images and match each one to the nearest step above it.

    Matching strategy (in priority order):
    1. Section-range matching: if section_ranges is provided (merged multi-section
       chunks), map image page → section title → best-matching step. This is reliable
       because section titles are structurally defined, not OCR-guessed.
    2. Positional text-block fallback: find the nearest text block above the image
       on the same page and fuzzy-match to a step.

    Returns (updated_steps, unmatched_assets).
    Failure never blocks the caller — returns (original_steps, []) on error.
    """
    import difflib

    try:
        import fitz  # PyMuPDF
        from .config import TASK_IMAGES_DIR
    except ImportError:
        logger.warning("PyMuPDF not installed — skipping image extraction for task %s", task_record_id[:8])
        return steps, []

    out_dir = os.path.join(TASK_IMAGES_DIR, task_record_id)
    seen_hashes: set[str] = set()
    unmatched: list[dict[str, Any]] = []

    steps_out = [dict(s) for s in steps]
    step_texts = [(i, (s.get("text") or "").strip().lower()) for i, s in enumerate(steps_out)]

    # Build page → section_title lookup from section_ranges
    page_to_section: dict[int, str] = {}
    if section_ranges:
        for sr in section_ranges:
            title = (sr.get("title") or "").strip().lower()
            for p in (sr.get("pages") or []):
                if isinstance(p, int):
                    page_to_section[p] = title

    def _best_step_match(candidate_text: str) -> tuple[int, float]:
        """Return (step_index, ratio) for the best-matching step, or (-1, 0)."""
        best_idx, best_ratio = -1, 0.0
        for i, st_text in step_texts:
            if not st_text:
                continue
            ratio = difflib.SequenceMatcher(None, st_text, candidate_text).ratio()
            if ratio > best_ratio:
                best_ratio, best_idx = ratio, i
        return best_idx, best_ratio

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("Could not open PDF for image extraction (%s): %s", pdf_path, exc)
        return steps, []

    try:
        for page_num in pages:
            page_idx = page_num - 1
            if page_idx < 0 or page_idx >= len(doc):
                continue
            page = doc[page_idx]

            text_blocks = [b for b in page.get_text("blocks") if b[6] == 0]

            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    img = doc.extract_image(xref)
                except Exception:
                    continue

                img_bytes = img.get("image") or b""
                if len(img_bytes) < _IMG_MIN_BYTES:
                    continue
                width = img.get("width", 0)
                height = img.get("height", 0)
                if width < _IMG_MIN_PX or height < _IMG_MIN_PX:
                    continue

                digest = hashlib.sha256(img_bytes).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)

                ext = img.get("ext", "png")
                filename = f"{digest[:16]}.{ext}"
                url = f"/task-images/{task_record_id}/{filename}"

                os.makedirs(out_dir, exist_ok=True)
                img_path = os.path.join(out_dir, filename)
                if not os.path.exists(img_path):
                    with open(img_path, "wb") as f:
                        f.write(img_bytes)

                matched = False

                # Strategy 1: section-range matching (page → section title → step)
                if page_num in page_to_section and step_texts:
                    section_title = page_to_section[page_num]
                    best_idx, best_ratio = _best_step_match(section_title)
                    # Lower threshold here — section titles are authoritative
                    if best_ratio >= 0.20 and best_idx >= 0:
                        if not steps_out[best_idx].get("screenshot"):
                            steps_out[best_idx]["screenshot"] = url
                            matched = True

                # Strategy 2: positional text-block fallback
                if not matched and step_texts:
                    try:
                        rects = page.get_image_rects(xref)
                        img_y = rects[0].y0 if rects else None
                    except Exception:
                        img_y = None

                    if img_y is not None:
                        above = [b for b in text_blocks if b[3] < img_y]
                        if above:
                            nearest_block = max(above, key=lambda b: b[3])
                            block_text = (nearest_block[4] or "").strip().lower()
                            best_idx, best_ratio = _best_step_match(block_text)
                            if best_ratio >= 0.35 and best_idx >= 0:
                                if not steps_out[best_idx].get("screenshot"):
                                    steps_out[best_idx]["screenshot"] = url
                                    matched = True

                if not matched:
                    unmatched.append({
                        "url": url,
                        "type": "image",
                        "label": f"page {page_num}",
                    })

    except Exception as exc:
        logger.warning("Image extraction failed for task %s: %s", task_record_id[:8], exc)
        doc.close()
        return steps, []
    finally:
        try:
            doc.close()
        except Exception:
            pass

    matched_count = sum(1 for s in steps_out if s.get("screenshot"))
    if matched_count or unmatched:
        logger.info(
            "Extracted images for task %s: %d matched to steps, %d unmatched",
            task_record_id[:8], matched_count, len(unmatched),
        )
    return steps_out, unmatched


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
- "task": describes one or more concrete procedures an operator would perform (including sections that describe multiple related procedures — these will be extracted as separate tasks)
- "ignore": administrative, introductory, legal, appendix, glossary, index, or no actionable procedural content

Return JSON only — no markdown, no commentary:
{"type": "task", "confidence": 0.0, "reason": "one sentence"}"""

_EXTRACT_TASK_SYSTEM = """You are extracting structured task records from a section of technical documentation.

## Field definitions

outcome: A single sentence in passive voice describing the observable end state after all steps are complete. Specific to this procedure.

facts: Background knowledge the learner needs about the subject matter before they can make sense of this task. The "what" — what are the components involved, what do they do, what are they for. This is not technical reference data (commands and port numbers belong in steps); it is the definitional understanding a learner needs so they are not confused about what they are working with. Can be short for simple tasks, long for complex ones. Write each as a complete sentence.
  Good: "Veeam Agent for Microsoft Windows is a backup agent installed locally on each Windows machine that Veeam will protect." / "iscsid is the iSCSI daemon that manages active iSCSI sessions on the local machine." / "open-iscsi is the Linux iSCSI initiator stack — the complete set of kernel modules and userspace tools that allow a Linux machine to connect to iSCSI targets."
  Bad: "The default iSCSI port is 3260." (technical trivia, not definitional knowledge) / "Run sudo apt install open-iscsi." (belongs in steps)

concepts: The specific reason THIS task must be performed — not a general description of the technology. Every task in a product has a different concept; if the concept you write could apply equally to a different task in the same product, it is too generic and must be rewritten. Ask: what specifically breaks, fails, or cannot happen if this particular task is skipped? Write in plain English. A substantive explanation of one or two paragraphs is expected; do not summarise to a single line. Implementation details and "by the way" information belong in step notes, not here.
  Good (for "Install Veeam Agent"): a paragraph explaining that Veeam uses an agent-based architecture — the Veeam server cannot back up a Windows machine unless an agent is running locally on it, because the agent is the only component that can interface with that machine's VSS and OS-level APIs. Without this installation step, no backup jobs targeting this machine can run.
  Good (for "Configure a backup job"): a paragraph explaining that installing the agent alone does not protect any data — the agent is passive until a job explicitly defines what to back up, where to store it, and when to run. Without a configured job, the machine remains unprotected even with the agent installed.
  Bad: any sentence that describes what the product does in general ("Veeam Agent provides backup and recovery capabilities...") — this would be true regardless of which task is being performed and tells the learner nothing about why this specific task is necessary.

dependencies: Specific preconditions that must be true before the operator can start. Full sentences.
  Good: "Ubuntu machine is accessible with sudo privileges." / "No backup jobs are currently running."

software_name: The name of the software product this content relates to, as it appears in the source document (e.g. "Veeam Agent for Microsoft Windows", "Ubuntu", "PostgreSQL"). null if not determinable.

software_version: The version of that software this content was written for, exactly as it appears in the source document — in headers, titles, footers, or version declarations (e.g. "6.1", "22.04 LTS", "v3.2.1"). null if the document does not state a version.

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

{"tasks":[{"id":"T001","title":"Install the iSCSI initiator utilities","outcome":"The open-iscsi package is installed and the iscsid service is running on the Ubuntu machine.","software_name":"open-iscsi","software_version":null,"procedure_name":"Install open-iscsi via apt","facts":["open-iscsi is the Linux iSCSI initiator stack — the complete set of kernel modules and userspace tools that allow a Linux machine to discover, connect to, and maintain sessions with iSCSI targets.","iscsid is the iSCSI daemon process; it runs in the background and manages all active iSCSI sessions on the local machine.","iscsiadm is the command-line management interface for iSCSI on Linux; it is installed as part of open-iscsi and is used for all subsequent iSCSI configuration and discovery operations."],"concepts":["Without open-iscsi installed, a Linux machine cannot participate in iSCSI at all — there is no driver to make connections, no daemon to manage sessions, and no tooling to configure targets. This is not a general statement about what iSCSI is; it is the specific reason this installation task must come first in any iSCSI workflow: every subsequent task (discovery, login, persistence, mounting) depends on this stack being present and running. Skipping or deferring this task makes all other iSCSI tasks impossible to perform."],"dependencies":["Ubuntu machine is accessible with sudo privileges.","Machine has internet or local repository access."],"irreversible":false,"steps":[{"text":"Update the package index.","completion":"Completes without error.","actions":["sudo apt update"],"notes":null},{"text":"Install the open-iscsi package.","completion":"Completes without error, confirming open-iscsi and iscsiadm are installed.","actions":["sudo apt install open-iscsi"],"notes":"If open-iscsi is already installed, apt will report 'open-iscsi is already the newest version' and no further action is required."},{"text":"Enable the iscsid service to start on boot.","completion":"Returns a symlink confirmation line.","actions":["sudo systemctl enable iscsid"],"notes":"On some Ubuntu versions, open-iscsi enables iscsid automatically on installation — if so, this command returns without output and no further action is needed."},{"text":"Start the iscsid service.","completion":"Returns to prompt without error.","actions":["sudo systemctl start iscsid"],"notes":null},{"text":"Confirm the service is active.","completion":"Output shows Active: active (running).","actions":["sudo systemctl status iscsid"],"notes":"On some minimal Ubuntu installations the service may show as 'inactive (dead)' immediately after install — if so, repeat the start command and check again."}]}]}"""


def _llm_triage_chunk(text: str, section_title: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Classify a chunk as task/ignore. Skips LLM for sparse chunks."""
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
        # "workflow" was a legacy category — treat as "task" for backwards compatibility
        if chunk_type == "workflow":
            chunk_type = "task"
        elif chunk_type not in ("task", "ignore"):
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
    """Extract schema 1.0 task fragment from a chunk. Always returns tasks only."""
    logger.info("Extracting tasks from '%s'", section_title[:80])
    user_msg = f"SECTION: {section_title}\n\nSOURCE TEXT:\n{(text or '').strip()}"
    raw = _llm_chat([
        {"role": "system", "content": _EXTRACT_TASK_SYSTEM},
        {"role": "user", "content": user_msg},
    ], cfg)
    result = _parse_llm_json(raw, section_title, int(cfg.get("llm_max_tokens") or 2000))
    if not isinstance(result.get("tasks"), list):
        result["tasks"] = []
    logger.info("Extracted %d task(s) from '%s'", len(result["tasks"]), section_title[:80])
    return result
