# Demo Data Expansion — Phase 2 Seeder Implementation Spec

Status: Draft for implementation
Depends on: `working_docs/demo-data-phase1-contract.md`

## 1) Objective

Build a deterministic seeder/reset tool that can generate a large, realistic `blueprinted_org` dataset with controlled pressure/integrity signals suitable for dashboard demos.

## 2) Entrypoint

Preferred script location:
- `lcs_mvp/scripts/seed_blueprinted_org.py`

Invocation examples:
- Plan only:
  - `python scripts/seed_blueprinted_org.py --profile blueprinted_org --plan`
- Reset + seed medium:
  - `python scripts/seed_blueprinted_org.py --profile blueprinted_org --reset --scale medium --seed 42`
- Reset + seed large custom:
  - `python scripts/seed_blueprinted_org.py --profile blueprinted_org --reset --tasks 1800 --workflows 520 --assessments 1100 --seed 1337`

## 3) CLI Contract

Required options:
- `--profile <key>` (default: `blueprinted_org`)
- `--seed <int>` (default fixed value for reproducibility)
- `--plan` (no writes; print intended volumes/distributions)
- `--reset` (wipe/rebuild target profile content safely)

Volume options:
- `--scale <small|medium|large>`
  - small: tasks 250 / workflows 80 / assessments 180
  - medium: tasks 900 / workflows 280 / assessments 650
  - large: tasks 1600 / workflows 480 / assessments 1000
- Optional overrides:
  - `--tasks <int>`
  - `--workflows <int>`
  - `--assessments <int>`

Distribution controls:
- `--task-status-profile <preset>`
- `--workflow-status-profile <preset>`
- `--assessment-status-profile <preset>`
- `--pressure-profile <balanced|high|spiky>`
- `--integrity-noise <none|low|medium>`

Safety options:
- `--confirm` required with `--reset` unless `--yes`
- `--yes` non-interactive execution

## 4) Domain Set (locked)

Use exactly:
- debian
- arch
- kubernetes
- aws
- postgres
- windows
- azure
- gcp
- terraform
- ansible
- vmware

## 5) Status Mix Defaults (latest-version state)

Tasks default:
- confirmed 55%
- draft 20%
- submitted 15%
- returned 10%

Workflows default:
- confirmed 50%
- draft 20%
- submitted 20%
- returned 10%

Assessments default:
- confirmed 60%
- draft 15%
- submitted 15%
- returned 10%

## 6) Taxonomy Rules

- Workflow-only tags.
- Tasks must be tagless in generated output.
- Intent belongs to workflows, not tasks.

## 7) Record Generation Rules

### 7.1 Tasks
- Generate reusable task corpus by domain.
- Keep varied title/outcome/procedure patterns.
- Ensure enough confirmed tasks exist to back workflow composition.

### 7.2 Workflows
- Each workflow references 3–8 tasks.
- Domain alignment must remain consistent with referenced tasks.
- Ensure meaningful split of submitted/returned/confirmed/draft.

### 7.3 Assessments
- Link to tasks/workflows realistically where model expects it.
- Keep domain assignments coherent.

## 8) Pressure Signal Shaping

Need visible admin pressure variety:
- Some domains green (score 0)
- Some amber (>0 and <8)
- Some red (>=8)

Control levers:
- submitted item density by domain
- returned ratios by domain
- workflow blocked-by-task rates by domain

Target behavior:
- Not all pressure concentrated in one domain
- Not uniform across all domains

## 9) Integrity Noise Policy

Default: `low`
- small percentage of missing-domain/invalid cases for realism
- bounded so dashboards remain credible

Suggested defaults:
- tasks missing domain: ~1%
- assessments missing domain: ~1–2%
- invalid workflows: ~1%

## 10) Reset Semantics

`--reset` must:
1. clear generated content tables (tasks/workflows/refs/assessments/audit artifacts as appropriate)
2. preserve required schema and baseline auth/users/domains seed state
3. repopulate with deterministic seeded data

Must print pre/post summary:
- record totals by type
- status distribution by type
- domain pressure preview
- integrity counts

## 11) Validation Gates (must pass)

After seed run, run checks:
- counts match requested scale ± small tolerance
- status distributions near configured targets
- workflow refs valid ratio above threshold
- workflow-only tags rule enforced
- domain pressure includes green/amber/red presence (for medium/large)

If validation fails:
- non-zero exit
- clear failure summary

## 12) Deliverables

- Seeder script implementation
- Usage docs in `working_docs/demo-data-seeding.md`
- One example command for medium and large profiles
- Validation output sample block

## 13) Deferred to Phase 3+

- temporal trend simulation
- admin-configurable pressure weighting
- advanced scenario packs (incident spikes, seasonal patterns)
