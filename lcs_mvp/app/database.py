from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request

from .config import (
    DATA_DIR, DB_DEBIAN_PATH, DB_DEMO_LEGACY_PATH, DB_OLD_DEBIAN_PATH, DB_BLANK_PATH,
    DB_PRODUCTION_PATH, DB_KEY_PRODUCTION,
    UPLOADS_DIR, EXPORTS_DIR,
    DB_KEY_COOKIE, DB_KEY_DEBIAN, DB_KEY_DEBIAN_ALIAS, DB_KEY_DEMO_ALIAS, DB_KEY_BLANK,
    DB_PATH_CTX, DB_KEY_CTX, DB_PROFILE_KEY_RE,
    PHASE1_OPERATIONAL_DOMAINS, SESSION_TTL_HOURS,
)
from .utils import _json_dump, _json_load


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def session_expires_iso() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# DB key / profile helpers
# ---------------------------------------------------------------------------

def _normalize_db_key(key: str | None) -> str:
    k = (key or "").strip().lower()
    if k in (DB_KEY_DEMO_ALIAS, DB_KEY_DEBIAN_ALIAS):
        return DB_KEY_DEBIAN
    return k


def _selected_db_key(request: Request | None = None) -> str:
    if request is None:
        return _normalize_db_key(DB_KEY_CTX.get())
    k = _normalize_db_key(request.cookies.get(DB_KEY_COOKIE) or DB_KEY_DEBIAN)
    if k == DB_KEY_PRODUCTION:
        return DB_KEY_PRODUCTION
    if k not in _available_db_keys():
        return DB_KEY_DEBIAN
    return k


def _db_path_for_key(key: str) -> str:
    key = _normalize_db_key(key)
    if key == DB_KEY_DEBIAN:
        return DB_DEBIAN_PATH
    if key == DB_KEY_BLANK:
        return DB_BLANK_PATH
    if key == DB_KEY_PRODUCTION:
        return DB_PRODUCTION_PATH
    return os.path.join(DATA_DIR, f"lcs_{key}.db")


def _available_db_keys() -> set[str]:
    return {DB_KEY_DEBIAN, DB_KEY_BLANK, *_list_custom_db_keys()}


def _db_profile_label(key: str) -> str:
    k = _normalize_db_key(key)
    if k == DB_KEY_DEBIAN:
        return "blueprinted org"
    if k == DB_KEY_BLANK:
        return "blank"
    return k.replace("_", " ")


def _list_custom_db_keys() -> list[str]:
    """Return custom db profile keys from files in DATA_DIR.

    Custom DB files are named: lcs_<key>.db
    Reserved keys blueprinted_org/debian/demo/blank are excluded.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    keys: list[str] = []
    for name in os.listdir(DATA_DIR):
        if not name.startswith("lcs_") or not name.endswith(".db"):
            continue
        if name in (os.path.basename(DB_DEBIAN_PATH), os.path.basename(DB_DEMO_LEGACY_PATH), os.path.basename(DB_BLANK_PATH), os.path.basename(DB_PRODUCTION_PATH)):
            continue
        key = name[len("lcs_") : -len(".db")].strip().lower()
        if not key:
            continue
        if key in (DB_KEY_DEBIAN, DB_KEY_DEBIAN_ALIAS, DB_KEY_DEMO_ALIAS, DB_KEY_BLANK, DB_KEY_PRODUCTION):
            continue
        if not DB_PROFILE_KEY_RE.match(key):
            continue
        keys.append(key)
    keys.sort()
    return keys


def _create_custom_db_profile(key: str) -> None:
    key = (key or "").strip().lower()
    if key in (DB_KEY_DEBIAN, DB_KEY_DEBIAN_ALIAS, DB_KEY_DEMO_ALIAS, DB_KEY_BLANK):
        raise HTTPException(status_code=400, detail="Reserved profile key")
    if not DB_PROFILE_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid profile key (use: a-z, 0-9, _, -)")

    os.makedirs(DATA_DIR, exist_ok=True)
    dst = _db_path_for_key(key)
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="Profile already exists")

    # Copy the blank DB as a template (schema + seeded users/domains; no content).
    if not os.path.exists(DB_BLANK_PATH):
        init_db_path(DB_BLANK_PATH)
        DB_PATH_CTX.set(DB_BLANK_PATH)
        with db() as conn:
            _seed_demo_users(conn)
            _seed_demo_domains(conn)
            _seed_demo_entitlements(conn)

    shutil.copyfile(DB_BLANK_PATH, dst)


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    """Open a connection to the currently selected DB (via context var).

    Notes:
    - FastAPI sync routes run in a threadpool; we open a fresh connection per request.
    - SQLite is single-writer; WAL + a busy timeout avoids spurious "database is locked"
      errors under light concurrency.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    path = DB_PATH_CTX.get()
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_KNOWN_TABLES = frozenset({"tasks", "workflows", "assessment_items", "users", "domains", "audit_log"})


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"_column_exists: unknown table {table!r}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _relink_avatars(conn: sqlite3.Connection) -> None:
    """Re-link avatar_path for users whose column is NULL/empty but whose
    avatar file already exists in UPLOADS_DIR/avatars/.

    Matches files named avatar__{safe_username}__*.{ext} — the same pattern
    used by profile_save. Picks the lexicographically latest file (timestamps
    embedded in the name sort correctly). Safe to run on every startup.
    """
    avatars_dir = Path(UPLOADS_DIR) / "avatars"
    if not avatars_dir.exists():
        return

    rows = conn.execute(
        "SELECT id, username FROM users WHERE COALESCE(avatar_path, '') = ''"
    ).fetchall()
    if not rows:
        return

    for row in rows:
        safe_user = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(row["username"]))[:48]
        prefix = f"avatar__{safe_user}__"
        matches = sorted(
            f for f in avatars_dir.iterdir()
            if f.is_file() and f.name.startswith(prefix)
        )
        if matches:
            conn.execute(
                "UPDATE users SET avatar_path=? WHERE id=?",
                (str(matches[-1].resolve()), int(row["id"])),
            )


def init_db_path(db_path: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    os.makedirs(EXPORTS_DIR, exist_ok=True)

    DB_PATH_CTX.set(db_path)

    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              record_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              status TEXT NOT NULL,

              title TEXT NOT NULL,
              outcome TEXT NOT NULL,
              facts_json TEXT NOT NULL,
              concepts_json TEXT NOT NULL,
              procedure_name TEXT NOT NULL,
              steps_json TEXT NOT NULL,
              dependencies_json TEXT NOT NULL,
              irreversible_flag INTEGER NOT NULL,
              task_assets_json TEXT NOT NULL,

              domain TEXT NOT NULL DEFAULT '',

              tags_json TEXT NOT NULL DEFAULT '[]',
              meta_json TEXT NOT NULL DEFAULT '{}',

              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              updated_by TEXT NOT NULL,
              reviewed_at TEXT,
              reviewed_by TEXT,
              change_note TEXT,

              needs_review_flag INTEGER NOT NULL DEFAULT 0,
              needs_review_note TEXT,

              PRIMARY KEY (record_id, version)
            );

            CREATE TABLE IF NOT EXISTS workflows (
              record_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              status TEXT NOT NULL,

              title TEXT NOT NULL,
              objective TEXT NOT NULL,

              domains_json TEXT NOT NULL DEFAULT '[]',
              tags_json TEXT NOT NULL DEFAULT '[]',
              meta_json TEXT NOT NULL DEFAULT '{}',

              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              updated_by TEXT NOT NULL,
              reviewed_at TEXT,
              reviewed_by TEXT,
              change_note TEXT,

              needs_review_flag INTEGER NOT NULL DEFAULT 0,
              needs_review_note TEXT,

              PRIMARY KEY (record_id, version)
            );

            CREATE TABLE IF NOT EXISTS workflow_task_refs (
              workflow_record_id TEXT NOT NULL,
              workflow_version INTEGER NOT NULL,
              order_index INTEGER NOT NULL,
              task_record_id TEXT NOT NULL,
              task_version INTEGER NOT NULL,

              PRIMARY KEY (workflow_record_id, workflow_version, order_index),
              FOREIGN KEY (workflow_record_id, workflow_version)
                REFERENCES workflows(record_id, version)
                ON DELETE CASCADE,
              FOREIGN KEY (task_record_id, task_version)
                REFERENCES tasks(record_id, version)
                ON DELETE RESTRICT
            );

            -- ---- Assessments (MVP) ----

            CREATE TABLE IF NOT EXISTS assessment_items (
              record_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              status TEXT NOT NULL,

              stem TEXT NOT NULL,
              options_json TEXT NOT NULL,
              correct_key TEXT NOT NULL,
              rationale TEXT NOT NULL DEFAULT '',

              claim TEXT NOT NULL DEFAULT 'fact_probe',
              domains_json TEXT NOT NULL DEFAULT '[]',
              lint_json TEXT NOT NULL DEFAULT '[]',
              refs_json TEXT NOT NULL DEFAULT '[]',

              tags_json TEXT NOT NULL DEFAULT '[]',
              meta_json TEXT NOT NULL DEFAULT '{}',

              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              updated_by TEXT NOT NULL,
              reviewed_at TEXT,
              reviewed_by TEXT,
              change_note TEXT,

              needs_review_flag INTEGER NOT NULL DEFAULT 0,
              needs_review_note TEXT,

              PRIMARY KEY (record_id, version)
            );

            CREATE TABLE IF NOT EXISTS assessment_refs (
              assessment_record_id TEXT NOT NULL,
              assessment_version INTEGER NOT NULL,
              order_index INTEGER NOT NULL,
              ref_type TEXT NOT NULL,
              ref_record_id TEXT NOT NULL,
              ref_version INTEGER NOT NULL,

              PRIMARY KEY (assessment_record_id, assessment_version, order_index),
              FOREIGN KEY (assessment_record_id, assessment_version)
                REFERENCES assessment_items(record_id, version)
                ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entity_type TEXT NOT NULL,
              record_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              action TEXT NOT NULL,
              actor TEXT NOT NULL,
              at TEXT NOT NULL,
              note TEXT
            );

            -- ---- Export artifacts (v0) ----
            CREATE TABLE IF NOT EXISTS export_artifacts (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              filename TEXT NOT NULL,
              path TEXT NOT NULL,
              sha256 TEXT NOT NULL,

              workflow_record_id TEXT NOT NULL,
              workflow_version INTEGER NOT NULL,
              task_refs_json TEXT NOT NULL,

              exported_at TEXT NOT NULL,
              exported_by TEXT NOT NULL,
              retention_days INTEGER NOT NULL DEFAULT 30
            );

            CREATE INDEX IF NOT EXISTS idx_export_artifacts_workflow ON export_artifacts(workflow_record_id, workflow_version);

            -- Local auth (demo-friendly; real auth may be added later)
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL,
              password_salt_hex TEXT NOT NULL,
              password_hash_hex TEXT NOT NULL,
              demo_password TEXT,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              disabled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              revoked_at TEXT,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            -- Domains (admin-managed registry) + per-user domain entitlements
            CREATE TABLE IF NOT EXISTS domains (
              name TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              disabled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_domains (
              user_id INTEGER NOT NULL,
              domain TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              PRIMARY KEY (user_id, domain),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY (domain) REFERENCES domains(name) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS ingestions (
              id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              source_sha256 TEXT NOT NULL,
              filename TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              cursor_chunk INTEGER NOT NULL DEFAULT 0,
              max_tasks_per_run INTEGER NOT NULL DEFAULT 10,
              note TEXT
            );

            CREATE TABLE IF NOT EXISTS ingestion_chunks (
              ingestion_id TEXT NOT NULL,
              chunk_index INTEGER NOT NULL,
              pages_json TEXT NOT NULL,
              text TEXT NOT NULL,
              llm_result_json TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (ingestion_id, chunk_index),
              FOREIGN KEY (ingestion_id) REFERENCES ingestions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ingestions_sha ON ingestions(source_sha256);

            CREATE TABLE IF NOT EXISTS system_settings (
              key        TEXT PRIMARY KEY,
              value      TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              updated_by TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status);

            -- ---- Gamification ----
            CREATE TABLE IF NOT EXISTS achievements (
              code TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              description TEXT NOT NULL,
              icon TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT 'general',
              enabled INTEGER NOT NULL DEFAULT 1,
              rules_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS user_achievements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL,
              achievement_code TEXT NOT NULL,
              awarded_at TEXT NOT NULL,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              UNIQUE(username, achievement_code),
              FOREIGN KEY (achievement_code) REFERENCES achievements(code)
            );

            CREATE INDEX IF NOT EXISTS idx_user_achievements_username ON user_achievements(username);
            """
        )

        # Lightweight migrations (prototype-friendly)
        if not _column_exists(conn, "tasks", "tags_json"):
            conn.execute("ALTER TABLE tasks ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        if not _column_exists(conn, "tasks", "meta_json"):
            conn.execute("ALTER TABLE tasks ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute("UPDATE tasks SET tags_json='[]' WHERE COALESCE(tags_json,'[]') <> '[]'")
        if not _column_exists(conn, "workflows", "domains_json"):
            conn.execute("ALTER TABLE workflows ADD COLUMN domains_json TEXT NOT NULL DEFAULT '[]'")
        if not _column_exists(conn, "workflows", "tags_json"):
            conn.execute("ALTER TABLE workflows ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        if not _column_exists(conn, "workflows", "meta_json"):
            conn.execute("ALTER TABLE workflows ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
        if not _column_exists(conn, "tasks", "domain"):
            conn.execute("ALTER TABLE tasks ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
        if not _column_exists(conn, "users", "display_name"):
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        if not _column_exists(conn, "users", "bio"):
            conn.execute("ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        if not _column_exists(conn, "users", "avatar_path"):
            conn.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT NOT NULL DEFAULT ''")
        if not _column_exists(conn, "users", "demo_password"):
            conn.execute("ALTER TABLE users ADD COLUMN demo_password TEXT")
        if not _column_exists(conn, "users", "disabled_at"):
            conn.execute("ALTER TABLE users ADD COLUMN disabled_at TEXT")
        if not _column_exists(conn, "tasks", "needs_review_flag"):
            conn.execute("ALTER TABLE tasks ADD COLUMN needs_review_flag INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "tasks", "needs_review_note"):
            conn.execute("ALTER TABLE tasks ADD COLUMN needs_review_note TEXT")
        if not _column_exists(conn, "tasks", "software_version"):
            conn.execute("ALTER TABLE tasks ADD COLUMN software_version TEXT")
        if not _column_exists(conn, "tasks", "software_name"):
            conn.execute("ALTER TABLE tasks ADD COLUMN software_name TEXT")
        if not _column_exists(conn, "workflows", "needs_review_flag"):
            conn.execute("ALTER TABLE workflows ADD COLUMN needs_review_flag INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "workflows", "needs_review_note"):
            conn.execute("ALTER TABLE workflows ADD COLUMN needs_review_note TEXT")
        if not _column_exists(conn, "assessment_items", "needs_review_flag"):
            conn.execute("ALTER TABLE assessment_items ADD COLUMN needs_review_flag INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "assessment_items", "needs_review_note"):
            conn.execute("ALTER TABLE assessment_items ADD COLUMN needs_review_note TEXT")

        # Session expiry migration
        rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        session_cols = {r["name"] for r in rows}
        if "expires_at" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")

        # ingestion_chunks: section_title, selection flag, per-chunk processing status, depth level
        rows = conn.execute("PRAGMA table_info(ingestion_chunks)").fetchall()
        chunk_cols = {r["name"] for r in rows}
        if "section_title" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN section_title TEXT")
        if "selected" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN selected INTEGER NOT NULL DEFAULT 0")
        if "chunk_status" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN chunk_status TEXT NOT NULL DEFAULT 'pending'")
        if "section_level" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN section_level INTEGER NOT NULL DEFAULT 0")
        if "chunk_type" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN chunk_type TEXT")
        if "triage_confidence" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN triage_confidence REAL")
        if "triage_reason" not in chunk_cols:
            conn.execute("ALTER TABLE ingestion_chunks ADD COLUMN triage_reason TEXT")

        # ingestions: overall async job state + stored file path for deletion
        rows = conn.execute("PRAGMA table_info(ingestions)").fetchall()
        ingestion_cols = {r["name"] for r in rows}
        if "job_status" not in ingestion_cols:
            conn.execute("ALTER TABLE ingestions ADD COLUMN job_status TEXT NOT NULL DEFAULT 'pending'")
        if "file_path" not in ingestion_cols:
            conn.execute("ALTER TABLE ingestions ADD COLUMN file_path TEXT")
        if "domain" not in ingestion_cols:
            conn.execute("ALTER TABLE ingestions ADD COLUMN domain TEXT NOT NULL DEFAULT ''")

        # Contributor role migration: collapse legacy author/reviewer into contributor
        conn.execute("UPDATE users SET role='contributor' WHERE role IN ('author', 'reviewer')")

        # Seed new system_settings defaults (INSERT OR IGNORE — never overwrite existing admin choices)
        now_iso = utc_now_iso()
        for _key, _default in [
            ("auth_mode", "demo"),
            ("auto_submit_on_import", "false"),
        ]:
            conn.execute(
                "INSERT INTO system_settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO NOTHING",
                (_key, _default, now_iso, "system"),
            )

        _backfill_workflow_domains(conn)
        _relink_avatars(conn)


# ---------------------------------------------------------------------------
# System settings / LLM config helpers
# ---------------------------------------------------------------------------

def _get_system_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_system_setting(conn: sqlite3.Connection, key: str, value: str, updated_by: str) -> None:
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO system_settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, value, now, updated_by),
    )


def _get_llm_config(conn: sqlite3.Connection) -> dict:
    """Return LLM provider config as a typed dict with safe defaults."""
    def _s(k: str, default: str = "") -> str:
        return _get_system_setting(conn, k, default) or default

    return {
        "llm_base_url":          _s("llm_base_url"),
        "llm_api_key":           _s("llm_api_key"),
        "llm_model":             _s("llm_model", ""),
        "llm_max_tokens":        int(_s("llm_max_tokens", "2000")),
        "llm_temperature":       float(_s("llm_temperature", "0.2")),
        "llm_timeout_seconds":   int(_s("llm_timeout_seconds", "120")),
        "llm_max_tasks_per_chunk": int(_s("llm_max_tasks_per_chunk", "5")),
        "llm_max_chunks_per_run": int(_s("llm_max_chunks_per_run", "8")),
    }


def _get_app_settings(conn: sqlite3.Connection) -> dict:
    """Return operational settings dict with safe defaults."""
    return {
        "auto_submit_on_import": (_get_system_setting(conn, "auto_submit_on_import", "false") or "false") == "true",
        "import_select_all": (_get_system_setting(conn, "import_select_all", "false") or "false") == "true",
    }


# ---------------------------------------------------------------------------
# Domain / workflow DB helpers
# ---------------------------------------------------------------------------

def _active_domains(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM domains WHERE disabled_at IS NULL ORDER BY name ASC").fetchall()
    return [str(r["name"]) for r in rows]


def _workflow_domains(conn: sqlite3.Connection, refs: list[tuple[str, int]]) -> list[str]:
    if not refs:
        return []
    domains: set[str] = set()
    for rid, ver in refs:
        r = conn.execute("SELECT domain FROM tasks WHERE record_id=? AND version=?", (rid, ver)).fetchone()
        d = (str(r["domain"]) if r else "").strip()
        if d:
            domains.add(d)
    return sorted(domains)


def _backfill_workflow_domains(conn: sqlite3.Connection) -> None:
    """Best-effort migration: populate workflows.domains_json for existing rows."""
    if not _column_exists(conn, "workflows", "domains_json"):
        return

    rows = conn.execute("SELECT record_id, version, domains_json FROM workflows").fetchall()
    for r in rows:
        try:
            existing = (r["domains_json"] or "").strip()
        except (TypeError, AttributeError):
            existing = ""
        if existing and existing != "[]":
            continue

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (r["record_id"], int(r["version"])),
        ).fetchall()
        pairs = [(x["task_record_id"], int(x["task_version"])) for x in refs]
        doms = _workflow_domains(conn, pairs)
        conn.execute(
            "UPDATE workflows SET domains_json=? WHERE record_id=? AND version=?",
            (_json_dump(doms), r["record_id"], int(r["version"])),
        )


# ---------------------------------------------------------------------------
# User DB helpers
# ---------------------------------------------------------------------------

def _user_id(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE username=? AND disabled_at IS NULL", (username,)).fetchone()
    return int(row["id"]) if row else None


def _user_has_domain(conn: sqlite3.Connection, username: str, domain: str) -> bool:
    if not domain:
        return False
    r = conn.execute("SELECT role FROM users WHERE username=? AND disabled_at IS NULL", (username,)).fetchone()
    if r and str(r["role"]) == "admin":
        return True
    uid = _user_id(conn, username)
    if uid is None:
        return False
    row = conn.execute("SELECT 1 FROM user_domains WHERE user_id=? AND domain=?", (uid, domain)).fetchone()
    return bool(row)


def _user_domains(conn: sqlite3.Connection, username: str) -> list[str]:
    """Return active domain entitlements for the user (sorted).

    Admin is treated as implicitly authorized for all domains.
    """
    r = conn.execute("SELECT role FROM users WHERE username=? AND disabled_at IS NULL", (username,)).fetchone()
    if r and str(r["role"]) == "admin":
        return _active_domains(conn)
    uid = _user_id(conn, username)
    if uid is None:
        return []
    rows = conn.execute(
        "SELECT domain FROM user_domains WHERE user_id=? ORDER BY domain ASC",
        (uid,),
    ).fetchall()
    return [str(x["domain"]) for x in rows if x and x["domain"]]


# ---------------------------------------------------------------------------
# Password + session helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt_hex: str) -> str:
    pw = (password or "").encode("utf-8")
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)
    return dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    return hmac.compare_digest(_hash_password(password, salt_hex), hash_hex)


def _new_session_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _seed_demo_users(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()

    demo = [
        ("jhendrix", "contributor", "password1"),
        ("jjoplin", "contributor", "password2"),
        ("wcarlos", "assessment_author", "password5"),
        ("awinehouse", "content_publisher", "password6"),
        ("fmercury", "viewer", "password3"),
        ("rjohnson", "audit", "password4"),
        ("kcobain", "admin", "admin"),
    ]

    def _rename(old: str, new: str) -> None:
        src = conn.execute("SELECT 1 FROM users WHERE username=?", (old,)).fetchone()
        dst = conn.execute("SELECT 1 FROM users WHERE username=?", (new,)).fetchone()
        if src and not dst:
            conn.execute("UPDATE users SET username=? WHERE username=?", (new, old))

    _rename("mcury", "fmercury")
    _rename("dspringsteen", "bspringsteen")
    _rename("bspringsteen", "rjohnson")
    _rename("admin", "kcobain")
    _rename("mcarey", "wcarlos")
    _rename("publisher", "awinehouse")

    for username, role, pw in demo:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET role=?, demo_password=?, disabled_at=NULL WHERE username=?",
                (role, pw, username),
            )
            continue
        salt = secrets.token_bytes(16).hex()
        conn.execute(
            """
            INSERT INTO users(username, role, password_salt_hex, password_hash_hex, demo_password, created_at, created_by)
            VALUES (?,?,?,?,?,?,?)
            """,
            (username, role, salt, _hash_password(pw, salt), pw, now, "seed"),
        )


def _seed_demo_domains(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    for d in PHASE1_OPERATIONAL_DOMAINS:
        conn.execute(
            "INSERT OR IGNORE INTO domains(name, created_at, created_by) VALUES (?,?,?)",
            (d, now, "seed"),
        )
        conn.execute("UPDATE domains SET disabled_at=NULL WHERE name=?", (d,))
    qmarks = ",".join(["?"] * len(PHASE1_OPERATIONAL_DOMAINS))
    conn.execute(
        f"UPDATE domains SET disabled_at=COALESCE(disabled_at, ?) WHERE name NOT IN ({qmarks})",
        [now, *PHASE1_OPERATIONAL_DOMAINS],
    )


def _seed_demo_entitlements(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()

    def uid(name: str) -> int | None:
        r = conn.execute("SELECT id FROM users WHERE username=? AND disabled_at IS NULL", (name,)).fetchone()
        return int(r["id"]) if r else None

    assignments: dict[str, list[str]] = {
        "jhendrix": ["debian"],
        "jjoplin": ["debian"],
    }
    for username, domains in assignments.items():
        u = uid(username)
        if not u:
            continue
        for d in domains:
            conn.execute(
                "INSERT OR IGNORE INTO user_domains(user_id, domain, created_at, created_by) VALUES (?,?,?,?)",
                (u, d, now, "seed"),
            )


def _seed_achievement_catalog(conn: sqlite3.Connection) -> None:
    """Upsert the starter achievement catalog. Safe to run on every startup."""
    catalog = [
        # ---- Author track: milestones ----
        ("first_draft",          "First Draft",             "Created your first task draft.",                              "✏️",  "author"),
        ("first_submit",         "First Submission",        "Submitted your first task for review.",                       "📤",  "author"),
        ("first_confirmed_task", "First Confirmed Task",    "Had your first task confirmed by a reviewer.",                "✅",  "author"),
        ("tasks_confirmed_5",    "Five Confirmed",          "Had 5 tasks confirmed.",                                      "🔔",  "author"),
        ("tasks_confirmed_10",   "Ten Confirmed",           "Had 10 tasks confirmed.",                                     "🥈",  "author"),
        ("tasks_confirmed_20",   "Twenty Confirmed",        "Had 20 tasks confirmed.",                                     "🥇",  "author"),
        ("tasks_confirmed_50",   "Fifty Confirmed",         "Had 50 tasks confirmed.",                                     "🏆",  "author"),
        # ---- Author track: quality ----
        ("revision_loop",        "Revision Loop",           "A task you authored was returned, revised, and confirmed.",   "🔁",  "author"),
        ("three_domain",         "Three-Domain Contributor","Had confirmed tasks across 3 distinct domains.",              "🌐",  "author"),
        ("first_workflow",       "First Workflow",          "Authored your first workflow.",                               "🗂️",  "author"),
        # ---- Reviewer track: milestones ----
        ("first_review",         "First Confirmation",      "Confirmed your first record.",                                "🔍",  "reviewer"),
        ("reviews_5",            "Five Reviews",            "Confirmed 5 records.",                                        "📋",  "reviewer"),
        ("reviews_10",           "Ten Reviews",             "Confirmed 10 records.",                                       "🥈",  "reviewer"),
        ("reviews_20",           "Twenty Reviews",          "Confirmed 20 records.",                                       "🥇",  "reviewer"),
        ("reviews_50",           "Fifty Reviews",           "Confirmed 50 records.",                                       "🏆",  "reviewer"),
        # ---- Reviewer track: quality ----
        ("hard_no",              "Hard No",                 "Returned a record for changes with a detailed note.",         "↩️",  "reviewer"),
        ("version_guardian",     "Version Guardian",        "Confirmed a record that supersedes a prior confirmed version.","🛡️", "reviewer"),
        ("multi_domain_review",  "Multi-Domain Rigor",      "Confirmed a workflow spanning 3 or more domains.",            "🗺️",  "reviewer"),
    ]
    for code, name, desc, icon, category in catalog:
        conn.execute(
            """
            INSERT INTO achievements(code, name, description, icon, category, enabled)
            VALUES (?,?,?,?,?,1)
            ON CONFLICT(code) DO UPDATE SET
              name=excluded.name,
              description=excluded.description,
              icon=excluded.icon,
              category=excluded.category,
              enabled=1
            """,
            (code, name, desc, icon, category),
        )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def _bootstrap_admin(conn: sqlite3.Connection) -> None:
    """Create a default admin/admin user if the DB has no users yet."""
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    now = utc_now_iso()
    salt = secrets.token_bytes(16).hex()
    pw = "admin"
    conn.execute(
        """
        INSERT INTO users(username, role, password_salt_hex, password_hash_hex, created_at, created_by)
        VALUES (?,?,?,?,?,?)
        """,
        ("admin", "admin", salt, _hash_password(pw, salt), now, "system"),
    )


def init_db() -> None:
    """Ensure the default blueprinted_org and blank DBs exist and are migrated."""
    if not os.path.exists(DB_DEBIAN_PATH) and os.path.exists(DB_OLD_DEBIAN_PATH):
        shutil.copy2(DB_OLD_DEBIAN_PATH, DB_DEBIAN_PATH)
    if not os.path.exists(DB_DEBIAN_PATH) and os.path.exists(DB_DEMO_LEGACY_PATH):
        shutil.copy2(DB_DEMO_LEGACY_PATH, DB_DEBIAN_PATH)

    init_db_path(DB_DEBIAN_PATH)
    init_db_path(DB_BLANK_PATH)
    init_db_path(DB_PRODUCTION_PATH)

    DB_PATH_CTX.set(DB_PRODUCTION_PATH)
    with db() as conn:
        _bootstrap_admin(conn)

    for p in (DB_DEBIAN_PATH, DB_BLANK_PATH):
        DB_PATH_CTX.set(p)
        with db() as conn:
            _seed_demo_users(conn)
            _seed_demo_domains(conn)
            _seed_demo_entitlements(conn)
            _seed_achievement_catalog(conn)

    # Migrate any existing custom databases so schema changes propagate to them too.
    for key in _list_custom_db_keys():
        custom_path = os.path.join(DATA_DIR, f"lcs_{key}.db")
        init_db_path(custom_path)
        DB_PATH_CTX.set(custom_path)
        with db() as conn:
            _seed_achievement_catalog(conn)


# ---------------------------------------------------------------------------
# Workflow readiness (pure DB query — used by analytics and route handlers)
# ---------------------------------------------------------------------------

def workflow_readiness_detail(conn: sqlite3.Connection, refs: list[tuple[str, int]]) -> dict[str, Any]:
    """Derived readiness + human-readable reasons.

    Rules:
      - If any reference is missing => invalid
      - If any reference is retired => invalid (no replacement)
      - Deprecated references are treated as historical/superseded and do not block readiness
      - Else if any reference is not confirmed (draft/submitted/returned) => awaiting_task_confirmation
      - Else => ready

    Returns:
      { readiness: str, reasons: [str], blocking_task_refs: [(rid,ver,status)] }
    """
    reasons: list[str] = []
    blocking: list[tuple[str, int, str]] = []

    if not refs:
        return {
            "readiness": "invalid",
            "reasons": ["Workflow has no Task references."],
            "blocking_task_refs": [],
        }

    awaiting = False

    for rid, ver in refs:
        row = conn.execute(
            "SELECT status, title FROM tasks WHERE record_id=? AND version=?", (rid, ver)
        ).fetchone()
        if not row:
            reasons.append(f"Missing Task reference: {rid}@{ver} does not exist")
            return {"readiness": "invalid", "reasons": reasons, "blocking_task_refs": blocking}

        st = row["status"]
        if st == "retired":
            reasons.append(f"Retired Task reference: {rid}@{ver} has no active replacement")
            return {"readiness": "invalid", "reasons": reasons, "blocking_task_refs": blocking}

        # Superseded historical task versions are allowed for existing approved workflows.
        if st == "deprecated":
            continue

        if st != "confirmed":
            awaiting = True
            blocking.append((rid, int(ver), str(st)))

    if awaiting:
        reasons.append("One or more referenced Task versions are not confirmed.")
        return {"readiness": "awaiting_task_confirmation", "reasons": reasons, "blocking_task_refs": blocking}

    return {"readiness": "ready", "reasons": [], "blocking_task_refs": []}


def workflow_readiness(conn: sqlite3.Connection, refs: list[tuple[str, int]]) -> Literal[
    "ready", "awaiting_task_confirmation", "invalid"
]:
    # Backward-compatible: keep readiness as a simple derived label.
    info = workflow_readiness_detail(conn, refs)
    return info["readiness"]


def enforce_workflow_ref_rules(conn: sqlite3.Connection, refs: list[tuple[str, int]]) -> None:
    """Hard constraints for workflow composition.

    Draft/submitted workflows may reference draft/submitted/confirmed Task versions.
    Confirmed workflows must reference confirmed Task versions only (enforced at confirm-time).

    Hard constraints here:
      - at least one task reference must exist
      - referenced task versions must exist
      - referenced task versions must not be retired
    """
    if not refs:
        raise HTTPException(status_code=400, detail="Workflow must include at least one Task reference")

    derived = workflow_readiness(conn, refs)
    if derived == "invalid":
        raise HTTPException(
            status_code=409,
            detail="Workflow contains invalid Task references (missing or retired task versions)",
        )
