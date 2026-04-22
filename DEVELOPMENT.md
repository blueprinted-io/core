# Project Loom — Development Guide

**Repository:** https://github.com/blueprinted-io/project-loom  
**Last Updated:** 2026-04-22
**Status:** MVP Active Development

---

## Quick Start (Local Development)

### Prerequisites
- Python 3.10+
- SQLite (bundled with Python)
- Git

### Setup

```bash
# Clone
git clone https://github.com/blueprinted-io/project-loom.git
cd project-loom/lcs_mvp

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or: .venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Initialize databases (first run only)
python -c "from app.main import init_db; init_db()"
```

### Running the Server

```bash
# Development (with auto-reload)
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production (no reload, daemonized)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Access at: http://localhost:8000

Default demo users (if seeded):
- `kcobain` / `admin` — Full admin access
- `jhendrix` / `password1` — Contributor: can create, submit, and review/confirm content
- `jjoplin` / `password2` — Contributor: can create, submit, and review/confirm content
- `wcarlos` / `password5` — Can create/maintain assessments
- `fmercury` / `password3` — Read-only, sees all confirmed content
- `rjohnson` / `password4` — Audit log access, sees all confirmed content
- `awinehouse` / `password6` — Delivery export access via `/delivery`

---

## Architecture Overview

### Stack
- **Backend:** FastAPI (Python)
- **Frontend:** Jinja2 server-rendered HTML templates
- **Database:** SQLite (`data/lcs_blueprinted_org.db` + `data/lcs_blank.db`)
- **Auth:** Session-based (HTTP-only cookies)
- **Styling:** Custom CSS (SPA-v1 theme in `static/style.css`)

### Key Directories
```
lcs_mvp/
├── app/
│   ├── main.py           # FastAPI app, routes, business logic
│   ├── templates/        # Jinja2 HTML templates
│   └── static/           # CSS, JS, images
├── data/                 # SQLite database (gitignored)
├── scripts/              # Utility scripts
└── requirements.txt      # Python dependencies
```

---

## Recent Changes (2026-04-22)

### Auth & Role Refactor
- **`contributor` role:** Replaced separate `author` and `reviewer` roles with a unified `contributor` role that can both create/submit and review/confirm content.
- **Self-review prohibition:** Contributors cannot confirm or return content they created (`created_by` check enforced server-side in all confirm/return handlers).
- **`auth_mode` setting:** Admin toggle at `/admin/rules`. `demo` = existing login splash with user cards; `production` = clean credential form only.
- **`auto_submit_on_import` setting:** Admin toggle at `/admin/rules`. When enabled, imported records (JSON + PDF) land as `submitted` instead of `draft`.
- **DB migration:** Existing users with `role = 'author'` or `role = 'reviewer'` are automatically migrated to `contributor` on first startup after upgrade.
- **Admin rules panel:** New `/admin/rules` page for managing `auth_mode` and `auto_submit_on_import` settings.

## Previous Changes (2026-02-28)

### Dashboard & Role System
- **Domain-agnostic roles:** `viewer`, `audit`, `content_publisher` now see all confirmed content across all domains (no domain assignment needed)
- **Per-domain breakdown table:** Domain-agnostic roles see a table showing confirmed tasks/workflows/assessments per domain with filter links
- **Health thresholds tightened:** Domain pressure now shows red <85%, amber 85-95%, green ≥95% (was 50/70)

### Admin & Profile
- **Disabled domain assignment:** Admin "Edit domains" button is disabled for domain-agnostic roles with explanatory tooltip
- **Backend enforcement:** API rejects domain assignment attempts for these roles (HTTP 400)
- **Profile page:** Domain section greyed out for domain-agnostic roles with explanation

### UI Polish
- **Author dashboard icons:** Create/import cards now have visual icons (⎇ ✓ ◈ 📄 {})
- **Disabled button styling:** Global `.btn:disabled` styling (opacity 0.4, not-allowed cursor)

### Explainer Page
- Added RAG (Retrieval-Augmented Generation) description for the system overview diagram

---

## Key Concepts

### Roles & Permissions

| Role | Domains | Can Create | Can Review | Notes |
|------|---------|------------|------------|-------|
| `admin` | All (implicit) | Yes | Yes | Full access, operational dashboard |
| `contributor` | Assigned only | Yes | Yes | Replaces former `author` + `reviewer` roles. Cannot review own content. |
| `assessment_author` | Assigned only | Assessments | No | Creates assessments, sees confirmed tasks/workflows |
| `viewer` | All (implicit) | No | No | Read-only, per-domain breakdown table |
| `audit` | All (implicit) | No | No | Audit log access, per-domain breakdown |
| `content_publisher` | All (implicit) | No | No | Export/publish, per-domain breakdown |

**Self-review prohibition:** A `contributor` cannot confirm or return content where `created_by` equals their own username. This is enforced in the backend route handlers for all confirm/return endpoints (tasks, workflows, assessments) — both the HTML routes and the `/api/*` JSON routes.

### Operational Settings (system_settings table)

| Key | Values | Default | Notes |
|-----|--------|---------|-------|
| `auth_mode` | `demo` / `production` | `demo` | Controls login page. Demo shows all users with passwords for one-click access. Production shows only a credential form. |
| `auto_submit_on_import` | `true` / `false` | `false` | When true, imported records (JSON + PDF) are created as `submitted` instead of `draft`. Imported records are never set to `confirmed`. |

Manage these at `/admin/rules` (admin only).

### Content Lifecycle

```
draft → submitted → confirmed
   ↑        ↓
   └──── returned
```

- **Draft:** Author working, not visible to others
- **Submitted:** Awaiting review
- **Returned:** Reviewer requested changes, back to author
- **Confirmed:** Approved, visible to viewer/audit/content_publisher
- **Deprecated:** Older confirmed version superseded
- **Retired:** Permanently removed (admin action)

### Domain Model

- **Domain:** A subject area (e.g., "kubernetes", "aws", "debian")
- **Task:** Smallest unit of work — outcome, facts, concepts, procedure steps
- **Workflow:** Ordered list of Tasks achieving one objective
- **Assessment:** Questions linked to Tasks/Workflows for learning verification

---

## Common Operations

### Restart Server After Code Changes

```bash
# Find and kill existing process
pkill -f "uvicorn app.main:app --host 0.0.0.0 --port 8000"

# Restart
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Run Automated Tests

```bash
pip install -r ../requirements-dev.txt
pytest -q
```

### Database Migrations

SQLite schema is auto-created on first run via `init_db()`. For schema changes:

1. Modify `CREATE TABLE` statements in `init_db()`
2. Run migration manually or recreate database

### Adding Corpus Data

```bash
# Debian corpus
python seed/seed_debian_corpus.py --reset-db

# Blueprinted demo corpus
python seed/seed_blueprinted_org.py --reset-db
```

---

## Development Notes

### CSS/Styling
- Main stylesheet: `app/static/style.css`
- SPA theme class: `body.spa-v1` (current active theme)
- Custom properties (CSS variables) defined in `:root`

### Template Structure
- Base: `templates/base.html`
- Dashboard: `templates/home.html` (role-conditional rendering)
- Entity pages: `templates/tasks_list.html`, `templates/task_edit.html`, `templates/workflows_list.html`, `templates/workflow_edit.html`, `templates/assessments_list.html`, `templates/assessment_edit.html`

### Debugging
- Server logs: `/tmp/lcs_mvp.log` (if using nohup) or terminal output
- Enable debug: Add `--reload` flag for auto-restart on code changes
- Databases: Inspect directly with `sqlite3 data/lcs_blueprinted_org.db` or `sqlite3 data/lcs_blank.db`

---

## Future Considerations

### React Rewrite (Post-MVP)
The current server-rendered approach is correct for MVP velocity. A full React rewrite is planned after:
1. Feature set is locked and validated
2. Domain model is stable
3. API endpoints are cleanly extracted

See `blueprinted-io-kimi-prompt.md` in workspace root for marketing site context (separate React project).

---

## Troubleshooting

### Port already in use
```bash
lsof -i :8000  # Find process
kill <PID>     # Terminate it
```

### Database locked
SQLite doesn't handle concurrent writes well. Ensure only one server process is running.

### Static files not updating
Browser may cache CSS. Hard refresh: `Ctrl+Shift+R` (or `Cmd+Shift+R` on Mac).

---

## Contact

- **Issues:** GitHub Issues
- **Docs:** This README + inline code comments
- **Slack/Discord:** #project-loom channel

---

_This guide is manually maintained and should match current code behavior._
