# UI Roadmap (LCS MVP)

## Current approach (pre-v1)

**Option A: CSS-only modernization.**

### Governance pulse (status strip)

Current (v1 MVP): global counts for Tasks/Workflows/Assessments, plus a reviewer pending count.

Target (post-v1): make pulse **user-context aware**:
- **Reviewer** sees only *their* actionable queue (domain-scoped), ideally broken down by entity type.
- **Admin** sees global operational totals (as today).

- Keep FastAPI + Jinja templates.
- Modernize the "app shell" look and feel via design tokens + CSS components.
- Prioritize clarity and speed for authoring/review workflows.

This is intentionally low-commitment while the domain model is still evolving.

## Post-v1 direction

**Option C: Dedicated front-end app (SPA).**

Once v1 stabilizes core entities (Tasks, Workflows, Assessments) and governance semantics:

- Introduce a dedicated front-end (e.g., React/Vue/Svelte).
- Split server into API + auth + rendering as needed.
- Improve information architecture and interaction patterns without fighting server-rendered templates.

Rationale: avoid over-investing in a UI architecture before the product intent and data model stop moving.
