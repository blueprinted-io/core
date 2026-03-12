# Staging Runbook (OpenClaw Uvicorn Host)

Purpose: keep staging deterministic and code-frozen.  
Policy: do not modify code on staging. Only pull from GitHub and restart the server.

## Scope

- Environment: OpenClaw host running `uvicorn app.main:app`
- Source of truth: `origin/main`
- Allowed actions: `git fetch/pull`, dependency install (if required), process restart, verification checks
- Not allowed: editing app code/templates/docs directly on staging

## Preconditions

- Repo already cloned on staging host
- Python virtual environment exists at `lcs_mvp/.venv`
- Required databases/assets already present for staging scenarios

## Standard Deploy Procedure

Run from repo root:

```bash
git fetch --tags origin
git pull --ff-only origin main
```

Install dependencies only when lock/requirements changed:

```bash
cd lcs_mvp
. .venv/bin/activate
pip install -r requirements.txt
cd ..
```

Restart uvicorn (example direct process):

```bash
pkill -f "uvicorn app.main:app" || true
cd lcs_mvp
. .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

If using a service manager, restart via that manager instead (systemd/supervisor/etc.).

## Post-Deploy Verification

1. App reachable:
- `GET /login` returns HTTP 200

2. Profile/database switching loads:
- Login as admin and open `/db`
- Confirm expected DB profiles are present

3. Core smoke flow:
- Create task (draft)
- Submit task
- Confirm task as reviewer
- Verify status transitions behave as expected

4. Optional regression check:
- From repo root: `python -m pytest -q tests` (if test deps installed on staging)

## Failure Handling

If `git pull --ff-only` fails:
- Do not resolve by editing in-place on staging.
- Stop and reconcile branch history from development environment.

If server fails to start:
- Check active interpreter/venv path
- Re-run `pip install -r lcs_mvp/requirements.txt`
- Verify DB file permissions and existence under `lcs_mvp/data/`

If DB appears locked:
- Ensure only one uvicorn process is running
- Remove stale `*.db-wal`/`*.db-shm` only after server is fully stopped

## Operational Notes

- Staging may intentionally contain richer runtime data/assets than local dev.
- Keep local development and staging data concerns separate.
- Treat staging as disposable runtime infrastructure; rebuild from Git + known data artifacts when needed.
