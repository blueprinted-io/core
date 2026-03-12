# Outstanding Ideas / Threads

Purpose: one-liners for what’s not finished, with pointers.

Keep this short and curated.

## In progress / MVP gaps

- PDF ingress reliability and UX hardening (chunking/cleanup/context pressure) — see `lcs_mvp/app/main.py` + `/import/pdf` UI.

- Reviewer feedback loop (Return for changes) is implemented; next: reviewer ergonomics (diffs, quick navigation) — see `/review` and task view.

## Future-state (planned)

- Output & Delivery module (renderer + provenance + “no new steps” enforcement) — docs: [Output_and_Delivery_Vision](Output_and_Delivery_Vision.md).
  - Export artifact retention cleanup is implemented as a repo script + **OS-level systemd timer**.
    - Script: `lcs_mvp/ops/cleanup_exports.py`
    - Unit files: `ops/systemd/project-loom-export-cleanup.{service,timer}`
    - Each environment must install/enable the timer (or schedule the script) to enforce age-out.
    - Admin-only on-demand run: `POST /admin/exports/cleanup`

- Achievements/gamification engine (event-sourced, auditable, no leaderboards by default) — docs: [Gamification_Achievements_Draft](Gamification_Achievements_Draft.md).

- Applicability/compatibility constraints (separate from domain/tags; roll up to workflows) — docs: [Applicability_and_Compatibility_Draft](Applicability_and_Compatibility_Draft.md).

- Interactive delivery packaging (H5P → SCORM/cmi5) — docs: [Interactive_Delivery_H5P_SCORM](Interactive_Delivery_H5P_SCORM.md).

- Static asset fingerprinting / cache-control (replace `style.css?v=...`) — see `lcs_mvp/README.md`.

- Semantic dedupe for ingress (move beyond near-duplicate heuristics) — related to PDF ingress + applicability.
