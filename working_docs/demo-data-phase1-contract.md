# Demo Data Expansion — Phase 1 Contract (Locked)

Status: Locked by agreement
Scope: Data contract only (pre-seeder implementation)

## 1) Profile / Dataset Identity

- Canonical org dataset key: `blueprinted_org`
- Purpose: large, realistic demo organization dataset

## 2) Domain Model (Operational Areas Only)

Domains are strictly operational areas.

Locked domain set:
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

## 3) Taxonomy Rules (Critical)

### 3.1 Tags
- Tags are **workflow-only**.
- Tasks are **tagless/agnostic** for Phase 1.

### 3.2 Intent vs Capability
- Workflow expresses **intent/context/outcome**.
- Task expresses **reusable capability/mechanic**, independent of workflow intent.

### 3.3 Explicit Exclusion
- `networking`, `security`, and `observability` are **not domains**.
- These may appear only in workflow-level tagging/taxonomy.

## 4) Data Realism Principles

- Dataset must support realistic admin pressure/health views.
- Pressure should be distributed across multiple domains (not single-domain concentrated).
- Include both healthy (green) and pressured (amber/red) domain states.
- Avoid degenerate demos where all metrics are zero or uniformly high.

## 5) Integrity/Noise Policy (Phase 1)

- Majority of records valid and internally consistent.
- Small intentional noise is allowed for realism in admin integrity checks.
- Integrity noise must remain bounded and deliberate (not random corruption).

## 6) Seeder Design Constraints (for next phase)

The seeder must eventually support:
- deterministic generation (seeded randomness)
- reproducible reset/rebuild runs
- explicit control of status/distribution ratios
- domain-aware load shaping
- pressure signal shaping for admin dashboard realism

## 7) Out of Scope (Phase 1)

- Time-series realism and trend simulation
- Admin-configurable dashboard composition
- Final v2 taxonomy governance model

---

This contract is the required baseline for Phase 2 seeder implementation.
