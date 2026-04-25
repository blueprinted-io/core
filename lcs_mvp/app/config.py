from __future__ import annotations

import contextvars
import os
import re
from typing import Literal

from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Status = Literal["draft", "submitted", "returned", "confirmed", "deprecated", "retired"]
Role = Literal["viewer", "contributor", "assessment_author", "content_publisher", "audit", "admin"]

# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_DEBIAN_PATH = os.path.join(DATA_DIR, "lcs_blueprinted_org.db")
DB_DEMO_LEGACY_PATH = os.path.join(DATA_DIR, "lcs_demo.db")
DB_OLD_DEBIAN_PATH = os.path.join(DATA_DIR, "lcs_debian.db")
DB_BLANK_PATH = os.path.join(DATA_DIR, "lcs_blank.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")
TASK_IMAGES_DIR = os.path.join(DATA_DIR, "task_images")

# ---------------------------------------------------------------------------
# Database key / profile constants
# ---------------------------------------------------------------------------

# Per-request DB selection via cookie; defaults to blueprinted_org.
DB_KEY_COOKIE = "lcs_db"
DB_KEY_DEBIAN = "blueprinted_org"
DB_KEY_DEBIAN_ALIAS = "debian"   # backward-compatible alias
DB_KEY_DEMO_ALIAS = "demo"       # backward-compatible alias
DB_KEY_BLANK = "blank"
DB_KEY_PRODUCTION = "production"
DB_PRODUCTION_PATH = os.path.join(DATA_DIR, "lcs_production.db")
DB_PATH_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("lcs_db_path", default=DB_DEBIAN_PATH)
DB_KEY_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("lcs_db_key", default=DB_KEY_DEBIAN)

DB_PROFILE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

PHASE1_OPERATIONAL_DOMAINS = [
    "debian",
    "arch",
    "kubernetes",
    "aws",
    "postgres",
    "windows",
    "azure",
    "gcp",
    "terraform",
    "ansible",
    "vmware",
]

# ---------------------------------------------------------------------------
# Operational constants
# ---------------------------------------------------------------------------

STALENESS_DAYS = 90   # confirmed content not reviewed within this threshold is considered stale
SESSION_TTL_HOURS = int(os.environ.get("LCS_SESSION_TTL_HOURS", "24"))
STATIC_ASSET_VERSION = "70"   # bump on each deploy to bust cached JS/CSS

# ---------------------------------------------------------------------------
# Jinja2 templates singleton (shared across all route modules)
# ---------------------------------------------------------------------------

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

import mistune as _mistune
_md_renderer = _mistune.create_markdown(
    plugins=["table", "strikethrough"],
    escape=True,
)
templates.env.filters["md"] = _md_renderer
