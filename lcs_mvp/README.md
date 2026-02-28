# blueprinted.io — Learning Content System MVP

FastAPI prototype implementing the core primitives from the docs:

- **Task** and **Workflow** as canonical, versioned records
- Status lifecycle: `draft → submitted → confirmed → deprecated`
- Confirmed records are immutable (edits create a new version)
- Workflow authoring may reference draft/submitted tasks; **workflow confirmation requires all referenced task versions confirmed**
- Warning-only step linting
- Exports: Markdown + JSON
- Imports: PDF (via LM Studio) + JSON

## Run locally

### Linux / macOS

```bash
cd lcs_mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open: http://127.0.0.1:8000

### Windows (PowerShell)

```powershell
cd lcs_mvp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Seed demo data (Debian corpus)

The demo dataset is designed to look like a real compliance system built around Debian/Linux operational procedures.

```bash
cd lcs_mvp
source .venv/bin/activate
python3 seed/seed_debian_corpus.py --reset-db
```

## Databases (MVP tenant switch)

This MVP uses **two local SQLite files**:

- Default demo DB: `lcs_mvp/data/lcs_blueprinted_org.db`
- Blank DB: `lcs_mvp/data/lcs_blank.db`

If you switch to the `admin` role in the UI, you can toggle between them via `/db`.

## Roles (prototype)

This is cookie-based role switching (not real authentication):

- `viewer` / `author` / `assessment_author` / `reviewer` / `content_publisher`: normal app roles (no audit log access)
- `audit`: read-only access, including audit log
- `admin`: can see everything; can also **force submit/confirm** (logged distinctly)

Notes:
- Delivery export from the `/delivery` page is restricted to `content_publisher`.
- Confirmed content export endpoints are intentionally readable by any authenticated role.
- Domain assignment is admin-managed (no self-service domain assignment on profile).

## Post-MVP TODOs

- Replace ad-hoc static cache-busting (e.g. `style.css?v=...`) with proper static asset fingerprinting / cache-control strategy.

## Notes

- PDF import expects a local LM Studio server if used (see `LCS_LMSTUDIO_BASE_URL`).
- This is an MVP prototype, not a hardened application.
