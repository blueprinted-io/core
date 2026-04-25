from __future__ import annotations

import itertools
import re
from typing import Any

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Verb sets used by the linter
# ---------------------------------------------------------------------------

ABSTRACT_VERBS = {
    "edit",
    "configure",
    "set up",
    "setup",
    "manage",
    "ensure",
    "handle",
    "prepare",
    "troubleshoot",
}

STATE_CHANGE_VERBS = {
    "install",
    "mount",
    "enable",
    "add",
    "update",
    "remove",
    "create",
    "delete",
}


# ---------------------------------------------------------------------------
# Step normalisation
# ---------------------------------------------------------------------------

def _normalize_steps(raw: Any) -> list[dict[str, Any]]:
    """Return steps as list of {text, completion, actions?, notes?}.

    Canonical meaning:
      - text: what you are doing (intent)
      - completion: how you prove the Step is complete (required)
      - actions: optional sub-instructions for how to perform the Step in a specific tool/environment

    Backward compatible with legacy storage:
      - steps as list[str]
      - steps as list[{text, completion}]
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                out.append({"text": item, "completion": "", "actions": [], "notes": "", "screenshot": None})
            elif isinstance(item, dict):
                actions_raw = item.get("actions")
                actions: list[str] = []
                if isinstance(actions_raw, list):
                    actions = [str(x) for x in actions_raw if str(x).strip()]
                elif isinstance(actions_raw, str):
                    # allow a single multi-line string
                    actions = [ln.strip() for ln in actions_raw.splitlines() if ln.strip()]

                notes = str(item.get("notes", "") or "").strip()
                screenshot = str(item.get("screenshot", "") or "").strip() or None
                out.append(
                    {
                        "text": str(item.get("text", "")),
                        "completion": str(item.get("completion", "")),
                        "actions": actions,
                        "notes": notes,
                        "screenshot": screenshot,
                    }
                )
        # Drop empty rows
        return [s for s in out if (s.get("text") or "").strip() or (s.get("completion") or "").strip()]
    return []


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

def lint_steps(steps: Any) -> list[str]:
    warnings: list[str] = []

    normalized = _normalize_steps(steps)

    for i, step in enumerate(normalized, start=1):
        s = step.get("text", "")
        low = s.strip().lower()

        notes = (step.get("notes") or "").strip()
        if notes:
            # Guardrail: notes are allowed but should not become shadow procedure.
            if len(notes) > 300 or re.search(r"\n\s*\d+\.", notes) or re.search(r"\b(step|run|then|next)\b", notes.lower()):
                warnings.append(
                    f"Step {i}: notes look instruction-heavy. Notes are for rare caveats/alternatives; move procedural content into Step/Actions."
                )

        # Abstract/bundling verbs
        for v in ABSTRACT_VERBS:
            if low.startswith(v + " ") or low == v:
                if not re.search(r"`.+?`", s) and not re.search(
                    r"\b(confirm|verify|check)\b", low
                ):
                    warnings.append(
                        f"Step {i}: starts with abstract verb '{v}'. Prefer decomposed steps with explicit method + completion check."
                    )
                break

        # Multi-action detector (refined): only warn when conjunctions likely hide multiple procedural operations.
        if re.search(r"\b(and|then|also|as well as)\b", low):
            verb_markers = (
                list(ABSTRACT_VERBS)
                + list(STATE_CHANGE_VERBS)
                + [
                    "run",
                    "open",
                    "copy",
                    "move",
                    "create",
                    "delete",
                    "set",
                    "insert",
                    "save",
                    "restart",
                    "reload",
                    "verify",
                    "confirm",
                    "record",
                    "list",
                    "check",
                    "edit",
                ]
            )
            # Count verb-like tokens appearing as word starts.
            hits = 0
            for v in set(verb_markers):
                if re.search(rf"\b{re.escape(v)}\b", low):
                    hits += 1
                if hits >= 2:
                    break
            if hits >= 2:
                warnings.append(
                    f"Step {i}: may include multiple procedural operations (conjunction + multiple verbs). Consider splitting."
                )

        # Verification expectation
        if any(low.startswith(v + " ") or low == v for v in STATE_CHANGE_VERBS):
            if not re.search(r"\b(confirm|verify|check)\b", low) and not re.search(r"`.+?`", s):
                warnings.append(
                    f"Step {i}: appears to change state; include an explicit confirmation check (command/UI observable) or follow with a check step."
                )

    return warnings


# ---------------------------------------------------------------------------
# Form → structured step helpers
# ---------------------------------------------------------------------------

def _zip_steps(
    step_text: list[str],
    step_completion: list[str],
    step_actions: list[str],
    step_notes: list[str] | None = None,
    step_screenshot: list[str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t, c, a, n, sc in itertools.zip_longest(
        step_text, step_completion, step_actions, step_notes or [], step_screenshot or [], fillvalue=""
    ):
        actions = [ln.strip() for ln in (a or "").splitlines() if ln.strip()]
        out.append(
            {
                "text": (t or "").strip(),
                "completion": (c or "").strip(),
                "actions": actions,
                "notes": (n or "").strip(),
                "screenshot": (sc or "").strip() or None,
            }
        )
    # Drop empty rows.
    return [
        s
        for s in out
        if (s.get("text") or "").strip()
        or (s.get("completion") or "").strip()
        or (s.get("actions") or [])
        or (s.get("notes") or "").strip()
    ]


def _validate_steps_required(steps: list[dict[str, Any]]) -> None:
    """Enforce step contract: step text + completion are required; actions are optional."""
    if not steps:
        raise HTTPException(status_code=400, detail="At least one step is required")
    for idx, st in enumerate(steps, start=1):
        if not (st.get("text") or "").strip():
            raise HTTPException(status_code=400, detail=f"Step {idx}: step text is required")
        if not (st.get("completion") or "").strip():
            raise HTTPException(status_code=400, detail=f"Step {idx}: completion text is required")
