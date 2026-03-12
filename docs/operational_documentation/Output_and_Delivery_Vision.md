# Output & Delivery (Vision)

Status: **draft**

This document describes the intended “delivery layer” for blueprinted.io: generating learning outputs (guides, courses, SOPs) from governed, human-verified Task/Workflow records.

The core purpose is to enable **hallucination-resistant learning content generation**.

---

## Why this exists

The strict Task/Workflow model is a *foundation*.

The end state is a delivery system where:

- Humans define and confirm canonical work once.
- The platform generates many learning views from that trusted data.
- The system can safely use LLMs for **flavour and connective tissue** without allowing them to invent procedure.

If the canonical data is correct, outputs are correct. If it is wrong, it is wrong once and can be fixed once.

---

## Core principles

### 1) Canonical data first

The governed Task/Workflow corpus is the source of truth.

- Steps are atomic and verifiable.
- Dependencies are explicit.
- Versions are immutable once confirmed.
- Audit trails exist.

### 2) Output is a render, not an authoring act

A “course” is a **derived artifact**.

The creator is selecting constraints and presentation format. They are not rewriting procedure.

### 3) LLMs are used, but constrained

LLMs are valuable for:

- tone and readability
- connective narration
- summarizing facts/concepts into “why it matters”
- adapting presentation to audience

LLMs must not be able to:

- invent new steps
- change step order or meaning
- introduce unverified commands
- quietly broaden scope

If the model cannot ground a claim to governed records, it must not produce the claim.

---

## Desired product flow (creator experience)

A “delivery builder” flow could look like:

1) Creator selects **target outcome**
   - choose one or more Workflows
   - (or choose an objective and have the system suggest workflows)

2) Creator supplies delivery constraints
   - target audience (role, baseline capability)
   - why they need it (business intent)
   - environment profile (e.g. Debian version, allowed tools, constraints)
   - style preset (Beginner guide / SOP / Checklist / Quick reference)
   - tone style guide (brand voice)

3) System validates readiness
   - workflow is confirmed (or flagged why it is not)
   - all referenced task versions are confirmed
   - domains/authorization constraints satisfied (future)

4) System generates outputs
   - Markdown / HTML / PDF
   - “course” view (narrative + progression)
   - SOP/checklist view (operational)
   - future: SCORM/cmi5/xAPI packages

5) System produces traceability artifacts
   - mapping: each section ↔ Task IDs/versions
   - “no new steps” verification report

---

## Output types (examples)

- **Beginner guide**
  - narrative, step-by-step with additional explanation
  - includes concepts in approachable language

- **Operator SOP**
  - terse, imperative, verification-heavy
  - optimized for execution under pressure

- **Checklist**
  - short lines, strong completion criteria

- **Job aid / quick reference**
  - minimal “why”, heavy “how”

- **Assessment items (future)**
  - questions derived from facts, concepts, and step intent
  - never generated from unconfirmed tasks

---

## LLM restraint model (non-optional)

LLMs are not optional because flavour matters, but **restraint is mandatory**.

### Constraining rules

1) **Grounding requirement**
   - Every generated section must cite the Task/Workflow IDs/versions that justify it.
   - The model must not output procedural instructions unless sourced from canonical Step data.

   Canonical Step data includes:
   - `step.text` (intent)
   - `step.completion` (proof)
   - optional `step.actions` (tool-specific how-to)

2) **Procedure immutability**
   - Step fields are copied from canonical records.
   - Allowed transformations:
     - formatting
     - adding clarifying *non-procedural* explanation
   - Disallowed transformations:
     - adding new procedural steps
     - merging steps into compound steps
     - replacing commands with new commands
     - inventing actions that are not present in canonical `step.actions` or an approved environment profile

3) **Unknowns must surface as gaps**
   - If a creator’s constraints require a missing prerequisite, the system must output:
     - “Missing Task/Dependency: …”
     - “Cannot generate this part without confirmed Task(s).”

4) **Environment profiles are authoritative**
   - Tool choices, command variants, and OS specifics should be driven by an environment profile.
   - The LLM must not invent environment-specific commands outside that profile.

### Two-layer generation (recommended)

- **Layer A: deterministic renderer**
  - assembles steps, facts, concepts, dependencies
  - generates baseline outputs without LLM

- **Layer B: LLM stylist**
  - rewrites only within strict boundaries
  - cannot change step content
  - produces “explanatory glue”

---

## Traceability and change control

Outputs must remain traceable and maintainable:

- Generated artifacts store:
  - input workflow IDs/versions
  - task versions used
  - generation parameters (audience, style guide)

- When a Task version updates:
  - derived outputs are flagged for regeneration
  - diffs are shown (what changed and why)

---

## What this document is not

- Not an implementation plan.
- Not a promise of immediate MVP scope.

This is the target state that explains why strict governance exists.

---

## Next questions (to design later)

- How environment profiles are represented (OS, tooling, constraints)
- How style guides are represented and validated
- Where “course intent” lives (objective framing, learner journey)
- How to compute and report “no hallucinated procedure” guarantees
