"""Deterministic achievement evaluator.

Called after every audit() write. Queries the audit_log and entity tables
to determine if any new badges should be awarded to the actor.

Design rules:
- Deterministic: derived entirely from recorded events, never manually set.
- Idempotent: INSERT OR IGNORE; safe to call multiple times.
- Non-blocking: exceptions are caught and logged; never crash the main flow.
- Count confirmed records (not raw submissions) for milestone badges.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .database import db, utc_now_iso

log = logging.getLogger(__name__)


def _award(conn: sqlite3.Connection, username: str, code: str, evidence: dict) -> bool:
    """Insert a user_achievement row. Returns True if newly awarded."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements(username, achievement_code, awarded_at, evidence_json)"
            " VALUES (?,?,?,?)",
            (username, code, utc_now_iso(), json.dumps(evidence)),
        )
        return conn.execute("SELECT changes()").fetchone()[0] == 1
    except Exception:
        return False


def _author_confirmed_count(conn: sqlite3.Connection, username: str) -> int:
    """Count distinct (record_id, version) task+assessment confirmations where the actor authored it."""
    # A task is "authored by" its created_by field; confirmed = audit action 'confirm'.
    t = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM audit_log al
        JOIN tasks t ON t.record_id = al.record_id AND t.version = al.version
        WHERE al.action = 'confirm'
          AND al.entity_type = 'task'
          AND t.created_by = ?
        """,
        (username,),
    ).fetchone()
    return int(t["c"]) if t else 0


def _reviewer_confirm_count(conn: sqlite3.Connection, username: str) -> int:
    """Count confirmations performed by this reviewer across all entity types."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE action='confirm' AND actor=?",
        (username,),
    ).fetchone()
    return int(row["c"]) if row else 0


def evaluate_achievements(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    entity_type: str,
    record_id: str,
    version: int,
) -> list[str]:
    """Evaluate and award any newly earned achievements. Returns list of newly awarded codes."""
    if not actor or actor == "system":
        return []

    awarded: list[str] = []

    try:
        # Determine actor's role
        role_row = conn.execute(
            "SELECT role FROM users WHERE username=? AND disabled_at IS NULL", (actor,)
        ).fetchone()
        role = str(role_row["role"]) if role_row else ""

        # ----------------------------------------------------------------
        # Author track
        # ----------------------------------------------------------------
        if action == "create" and entity_type == "task":
            if _award(conn, actor, "first_draft", {"record_id": record_id, "version": version}):
                awarded.append("first_draft")

        if action == "submit" and entity_type == "task":
            # first_submit: actor submitted at least one task
            prior = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_log WHERE action='submit' AND actor=? AND entity_type='task'",
                (actor,),
            ).fetchone()
            if prior and int(prior["c"]) == 1:  # this is the first submit
                if _award(conn, actor, "first_submit", {"record_id": record_id, "version": version}):
                    awarded.append("first_submit")

        if action == "create" and entity_type == "workflow":
            if _award(conn, actor, "first_workflow", {"record_id": record_id, "version": version}):
                awarded.append("first_workflow")

        if action == "confirm" and entity_type == "task":
            # Award milestone badges to the task's author (not the reviewer)
            t_row = conn.execute(
                "SELECT created_by FROM tasks WHERE record_id=? AND version=?", (record_id, version)
            ).fetchone()
            if t_row:
                author = str(t_row["created_by"])
                count = _author_confirmed_count(conn, author)

                if count == 1:
                    if _award(conn, author, "first_confirmed_task", {"record_id": record_id, "version": version}):
                        awarded.append("first_confirmed_task")
                for threshold, code in ((5, "tasks_confirmed_5"), (10, "tasks_confirmed_10"),
                                        (20, "tasks_confirmed_20"), (50, "tasks_confirmed_50")):
                    if count == threshold:
                        if _award(conn, author, code, {"count": count}):
                            awarded.append(code)

                # revision_loop: same record_id was previously returned and resubmitted
                returned = conn.execute(
                    "SELECT COUNT(*) AS c FROM audit_log"
                    " WHERE entity_type='task' AND record_id=? AND action='return_for_changes'",
                    (record_id,),
                ).fetchone()
                if returned and int(returned["c"]) > 0:
                    if _award(conn, author, "revision_loop", {"record_id": record_id, "version": version}):
                        awarded.append("revision_loop")

                # three_domain: author has confirmed tasks across 3+ distinct domains
                dom_count = conn.execute(
                    """
                    SELECT COUNT(DISTINCT t.domain) AS c
                    FROM audit_log al
                    JOIN tasks t ON t.record_id=al.record_id AND t.version=al.version
                    WHERE al.action='confirm' AND al.entity_type='task'
                      AND t.created_by=? AND t.domain != ''
                    """,
                    (author,),
                ).fetchone()
                if dom_count and int(dom_count["c"]) >= 3:
                    if _award(conn, author, "three_domain", {"domains_count": int(dom_count["c"])}):
                        awarded.append("three_domain")

        # ----------------------------------------------------------------
        # Reviewer track
        # ----------------------------------------------------------------
        if action == "confirm":
            count = _reviewer_confirm_count(conn, actor)

            if count == 1:
                if _award(conn, actor, "first_review", {"record_id": record_id, "entity_type": entity_type}):
                    awarded.append("first_review")
            for threshold, code in ((5, "reviews_5"), (10, "reviews_10"),
                                    (20, "reviews_20"), (50, "reviews_50")):
                if count == threshold:
                    if _award(conn, actor, code, {"count": count}):
                        awarded.append(code)

            # version_guardian: this record_id has a prior confirmed version
            if entity_type in ("task", "workflow", "assessment_items"):
                table = entity_type if entity_type != "assessment_items" else "assessment_items"
                prior_confirmed = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"
                    " WHERE record_id=? AND version<? AND status='confirmed'",
                    (record_id, version),
                ).fetchone()
                if prior_confirmed and int(prior_confirmed["c"]) > 0:
                    if _award(conn, actor, "version_guardian", {"record_id": record_id, "version": version}):
                        awarded.append("version_guardian")

            # multi_domain_review: confirmed a workflow spanning 3+ domains
            if entity_type == "workflow":
                wf = conn.execute(
                    "SELECT domains_json FROM workflows WHERE record_id=? AND version=?",
                    (record_id, version),
                ).fetchone()
                if wf:
                    try:
                        doms = json.loads(wf["domains_json"] or "[]")
                    except Exception:
                        doms = []
                    if len(doms) >= 3:
                        if _award(conn, actor, "multi_domain_review", {"record_id": record_id, "domains": doms}):
                            awarded.append("multi_domain_review")

        if action == "return_for_changes":
            # hard_no: reviewer returned with a non-trivial note (len > 10 chars)
            note_row = conn.execute(
                "SELECT note FROM audit_log"
                " WHERE entity_type=? AND record_id=? AND version=? AND action='return_for_changes'"
                " ORDER BY id DESC LIMIT 1",
                (entity_type, record_id, version),
            ).fetchone()
            note = str(note_row["note"] or "") if note_row else ""
            if len(note.strip()) > 10:
                if _award(conn, actor, "hard_no", {"record_id": record_id, "version": version}):
                    awarded.append("hard_no")

    except Exception as exc:
        log.warning("achievements: evaluation error for actor=%r action=%r: %s", actor, action, exc)

    return awarded


def get_user_achievements(conn: sqlite3.Connection, username: str) -> list[dict]:
    """Return earned achievements for a user, joined with catalog metadata."""
    rows = conn.execute(
        """
        SELECT ua.achievement_code, ua.awarded_at, ua.evidence_json,
               a.name, a.description, a.icon, a.category
        FROM user_achievements ua
        JOIN achievements a ON a.code = ua.achievement_code
        WHERE ua.username = ? AND a.enabled = 1
        ORDER BY ua.awarded_at DESC
        """,
        (username,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "code": r["achievement_code"],
            "name": r["name"],
            "description": r["description"],
            "icon": r["icon"],
            "category": r["category"],
            "awarded_at": r["awarded_at"],
        })
    return out
