# Interactive Delivery: H5P → (SCORM / cmi5) (Provisional)

Status: **provisional / exploration**

This document captures an exploratory idea:

- Use the governed Task/Workflow corpus as the **ground truth substrate**.
- Generate interactive learning objects using **H5P content types**.
- Package/track via **SCORM** (broad compatibility) and/or **cmi5** (modern xAPI profile).

The objective is not “AI creates training”. The objective is:

> Humans confirm canonical procedure once; the platform safely renders many interactive formats without hallucinated steps.

---

## Why H5P fits this architecture

H5P is, at its best, a library of structured interactive templates.

That aligns naturally with a database-defined corpus:

- **Facts** → recall questions
- **Concepts** → explanations and mental model reinforcement
- **Steps** → sequencing, ordering, and verification-heavy interactions

The critical requirement is that interactivity remains **grounded** in confirmed records.

---

## Proposed pipeline (conceptual)

### Inputs (creator-facing)

- selected Workflow(s)
- target audience and baseline assumptions
- objective framing (what capability at the end)
- delivery format preset (assessment-heavy / practice-heavy / operator refresher)
- style guide / tone
- environment profile (future)

### Layer A: deterministic extraction (non-LLM)

Derive a structured “interaction plan” from canonical data:

- section outline
- which task versions are in scope
- which facts/concepts are eligible for questions
- which steps are eligible for sequencing checks
- any safety flags (irreversible tasks, privileged actions)

**Output:** a machine-readable plan with explicit provenance to record_id@version.

### Layer B: constrained LLM enrichment (optional)

LLM can be used for:

- wording variations
- distractor phrasing (bounded)
- short explanations (“why this matters”) derived from Concepts

LLM must not:

- invent new steps
- introduce new commands/tooling
- alter step order or meaning

**Output:** H5P-ready payloads where the procedural substrate is still deterministic.

### Layer C: packager

- produce H5P content JSON + assets
- package as:
  - direct H5P bundle (where supported)
  - SCORM package (for LMS compatibility)
  - cmi5 package (for modern tracking)

---

## H5P content types that map cleanly from Tasks/Workflows

This is intentionally approximate; the exact mapping should be refined after licensing and technical validation.

### Good fits

1) **Drag & Drop / Drag the Words**
- Map from Facts and Concepts (terminology, definitions)

2) **Fill in the Blanks (Cloze)**
- Map from Facts (commands, file paths, config keys)
- Must be careful not to train “copy/paste incantations” without context

3) **Question Set / Multi-choice**
- Map from Facts (recall)
- Some Concepts (why/behavior) as scenario prompts

4) **Mark the Words**
- Map from Concepts text or policy statements

5) **Sequence / Ordering** (if available via H5P type or custom)
- Map from Task Steps (ordering and dependencies)

6) **Flashcards**
- Map from Facts and small Concepts

### Less safe / needs more design

- **Branching Scenario**
  - Risk: pushes into diagnostics/troubleshooting (explicitly out of scope for canonical Steps)
  - Might be used only for “policy and decision constraints” if those are formalized as separate record types

- **Interactive Video**
  - Asset heavy; content governance and localization become bigger problems

---

## Grounding and provenance (non-negotiable)

Every generated interactive object should carry provenance metadata:

- which Workflow record_id@version
- which Task record_id@version
- which fact/concept/step IDs were used

This enables:

- auditability
- regen on task updates
- “no hallucinated procedure” enforcement

---

## Packaging: SCORM vs cmi5

### SCORM

Pros:
- universal LMS support
- easiest enterprise compatibility story

Cons:
- limited tracking semantics
- legacy constraints

### cmi5 (xAPI profile)

Pros:
- more modern tracking
- better structure than raw xAPI

Cons:
- less universally supported

Recommendation (eventual): generate both from the same canonical plan.

---

## Licensing and commercial constraints (TODO)

This section is intentionally incomplete.

We need to validate:

- H5P core licensing
- editor/hosting licensing differences
- whether certain tooling requires commercial licenses
- constraints on distributing generated packages

Until this is clear, treat H5P as an architectural *candidate*, not a committed dependency.

---

## Next experiments (when ready)

1) Pick 1 workflow from the Debian corpus and derive:
   - 10 facts → 10 MCQs
   - 1 task → 1 sequencing interaction

2) Create an “interaction plan” JSON schema that includes provenance.

3) Validate we can package and launch in a SCORM test harness.

4) Only then decide whether H5P is the right interactive engine.
