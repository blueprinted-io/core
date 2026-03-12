# Applicability and Compatibility (Draft)

Status: draft / future-state.

## Problem

Procedures are not universally valid.

Two Tasks can both be in the **Linux domain** but differ in applicability:

- One works on any modern Linux distribution.
- One is specific to Debian 12.
- One requires systemd.
- One is specific to a product version (e.g., Kubernetes 1.30).

If the system cannot represent applicability, it will:

- publish misleading procedure,
- create accidental duplicates,
- and force authors to encode compatibility inside titles/tags (guaranteed drift).

## Definitions (separation of concerns)

- **Domain**: authorization boundary.
  - Answers: who is allowed to author/review/confirm this content?
  - Example: `linux`, `kubernetes`, `windows`.

- **Tags**: discovery labels.
  - Answers: how do humans find and group content?
  - Examples: `operations`, `packaging`, `assurance`.

- **Applicability / Compatibility**: validity boundary.
  - Answers: where/when does this procedure work?
  - This is not a tag. It is a constraint.

## Scope

Applicability is intended to express **platform and version validity**, not troubleshooting.

- In scope:
  - OS family and distro (if relevant)
  - Product/platform versions
  - required components (e.g., init system, package manager)
  - environment class (cloud/on‑prem) if relevant

- Out of scope:
  - diagnostics/fault isolation
  - scenario-specific “it depends” decision trees

## Neutral vocabulary (content-agnostic)

Applicability should be able to represent non-Linux domains too.

A generic set of constraint axes:

- `platform`: the broad execution environment.
  - Examples: `linux`, `windows`, `macos`, `kubernetes`, `aws`, `gcp`, `saas`.

- `distribution` / `variant`: optional sub-classification.
  - Examples: `debian`, `ubuntu`, `rhel`, `eks`, `aks`, `gke`.

- `version`: a range or set.
  - Examples: `>= 12`, `>= 1.29 < 1.31`, `2026.1`.

- `components`: optional required components.
  - Examples: `systemd`, `apt`, `dnf`, `bash`, `powershell`.

- `constraints`: additional boolean requirements.
  - Examples: `requires_root=true`, `requires_network=true`.

This is intentionally minimal. Overfitting the vocabulary early causes drift.

## Data model direction (post-MVP)

### Task-level field

Add a dedicated field on Tasks:

- `applicability_json` (preferred) or `applicability_note` (fallback)

Why JSON is preferred:

- queryable filters (UI + export)
- dedupe support
- workflow rollups
- prevents “hide compatibility inside title/tag”

Example shape (illustrative, not final):

```json
{
  "platform": ["linux"],
  "distribution": ["debian"],
  "version": {"min": "12", "max_exclusive": "13"},
  "components": ["systemd", "apt"],
  "constraints": {"requires_root": true}
}
```

### Workflow rollup

Workflows reference Tasks; applicability must roll up.

Two possible rollup semantics:

- **Union (warning-driven)**: workflow is valid where any of its tasks are valid, but UI must warn when tasks disagree.
- **Intersection (strict)**: workflow is only valid where all referenced tasks are valid.

Default recommendation:

- Store workflow applicability as a derived field.
- Present conflicts explicitly (do not hide them).

## Governance rules

- Applicability is part of the **review contract**.
- Missing applicability should be treated as **unknown**, not “any”.

MVP posture (documentation-level):

- Allow drafts to omit applicability.
- For confirmation, either:
  - require applicability, or
  - allow omission but add a prominent “unknown applicability” warning.

## Interaction with duplication checks

Applicability affects duplication in two ways:

1) Identical procedures for different versions are not duplicates.
2) Near-duplicate detection should consider applicability before flagging a duplicate.

## Ingress posture (PDF/JSON)

- Import should never assume applicability.
- Ingress may propose a best-effort applicability, but it must be treated as draft and reviewed.

## Why this exists

Applicability keeps the system honest:

- It prevents “works on my machine” procedures from being published as universal.
- It prevents tags from being abused as compatibility fields.
- It makes dedupe and safe delivery possible.
