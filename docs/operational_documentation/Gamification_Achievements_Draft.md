# Gamification (Author + Reviewer) — Achievements Draft

Status: draft / future-state.

Goal: make authoring + review feel rewarding without compromising governance quality.
Reference: Untappd (badges for milestones, combinations, streaks, novelty).

## Non-negotiable principles

1) **Reward compliance and quality, not just throughput.**
   - Badges must not incentivize low-quality submissions or rubber-stamp confirmations.

2) **Deterministic and auditable.**
   - Achievements are awarded from recorded events (audit log), not manually edited fields.

3) **Achievements are optically internal.**
   - This is an internal system; privacy optics are acceptable.
   - **Leaderboards are not a default feature** (optional later, likely not helpful).

4) **Role-aware, domain-aware.**
   - Author and reviewer tracks are different.
   - Domain breadth badges should avoid pushing authors outside entitlements.

## Data model direction (event-sourced)

Source of truth: `audit_log` + a small set of additional review events.

Proposed tables (post-MVP):

- `achievements`
  - `id` (pk)
  - `code` (unique stable identifier)
  - `name`
  - `description`
  - `icon` / `style` (optional)
  - `rarity` (optional)
  - `enabled`
  - `rules_json` (deterministic rule spec)

- `user_achievements`
  - `user_id`
  - `achievement_code` (or `achievement_id`)
  - `awarded_at`
  - `evidence_json` (the event ids/record refs that triggered it)

Optional:
- `user_stats` materialized counters for fast UI (derived from events).

## Event hooks

Existing events we already log:
- task: create/new-version/submit/confirm/force-*
- workflow: submit/confirm/force-*
- admin user/domain operations

Likely additions to support quality-oriented reviewer achievements:
- `task:return_for_changes` (with structured reason)
- `task:reject` (rare; but useful)
- `review_note:add` (structured findings; severity optional)

## Badge catalog (starter)

### Author track (examples)

Milestones:
- First Draft Created
- First Submission
- First Confirmed Task
- Confirmed Task Milestones: 5 / 10 / 20 / 50 / 100

Quality:
- Clean Submit: submitted with 0 lint warnings
- Completion-Forward: every step has a completion check (already required; keep as “you did it right”)
- Actions Present: every step includes optional `actions` (where applicable)

Domains:
- First Confirmed in a New Domain
- Three-Domain Contributor (3 distinct domains with confirmed tasks)

Combos (Untappd-style):
- Revision Loop: submitted → returned → revised → confirmed (same record)
- Streaks: confirmed tasks on 5 different days

### Reviewer track (examples)

Milestones:
- First Confirmation
- Confirmed Reviews: 5 / 10 / 20 / 50 / 100

Quality / governance:
- Hard No: returned for changes with clear reason (requires new event)
- Multi-Domain Rigor: confirmed workflow spanning N domains
- Version Guardian: confirmed a record that supersedes a previously confirmed version (normal, but badge once)

Combos:
- Triage Sprint: cleared X pending reviews in a session (bounded)

## UI direction (post-MVP)

- Small toast on award (no spam; rate-limited).
- Profile “cabinet”: badges + stats.
- Badge detail: what triggered it (evidence).
- No leaderboards by default.

## Anti-perverse-incentive guardrails

- Prefer counting **confirmed** (reviewed) records for milestones.
- For reviewer badges, avoid rewarding only “confirm volume”; include “returned/rejected with reason” badges.
- Rate-limit awards (e.g., max N per day) to prevent spam.

## Implementation plan (post-MVP)

1) Add missing review events (return/reject/structured note).
2) Add achievement tables.
3) Implement a deterministic rules engine that consumes audit events.
4) UI: cabinet + toasts + “earned because…” details.
