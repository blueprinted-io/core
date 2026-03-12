# Learning Content System Design

This document defines the data model, schema, and system structure for
database-defined learning content. It complements
[Learning Content Governance](Learning_Content_Governance.md), which covers standards, governance,
and review rules.

The application exists to maintain a governed, versioned, and structurally validated corpus of procedural performance units that can be safely reused, reviewed, and aligned to organizational objectives.

These constraints exist to support safe, hallucination-resistant generation of learning outputs (guides/courses/SOPs) from confirmed records. See: [Output & Delivery (Vision)](../operational_documentation/Output_and_Delivery_Vision.md).

## MVP Scope (System Design)

The MVP supports:

- Create Tasks, validate, and sequence them into Workflows.

- Export Workflows as Markdown, HTML, PDF, and raw data.

- Web UI authoring only (no API in MVP).

- Provide a validated JSON schema for manual import.

Out of scope for MVP:

- Assessment engine (future use of content as AI question bank source).

- Diagnostics and troubleshooting content.

- Localization.

- Asset library hosting (external URLs only for now).

- Agentic authoring (manual authoring only).

# Learning Content Standards (Database‑Defined)

## Purpose

Learning content is database‑defined. The database records form the
canonical, learner‑facing source of truth for all procedural learning.
All delivery methods; written guides, videos, interactive walkthroughs,
live labs, ILT, job aids, SCORM, derive from these database definitions
for accuracy and structure.

A data‑defined record provides:

1.  **Outcome** – the title stated as a verb-driven outcome.

2.  **Facts** – literal information the learner must know beforehand.

3.  **Concepts** – the mental models required to understand why the
    steps work.

4.  **Procedure** – the name of the step sequence that performs the
    task.

5.  **Dependencies** – conditions that must already be true or
    completed.

Steps are stored in a separate Steps table linked to the Task version.
The Procedure is a name field that describes the step sequence.

A workflow record provides:

1.  **Objective** – the measurable outcome defined by the organization.

2.  **Tasks** – ordered task references that produce the objective.

Workflow composition (for authoring) may reference Task versions in draft, submitted, or confirmed state.

**Confirmation constraint:** a Workflow may transition to **confirmed** only if every referenced Task version is **confirmed**. Draft/submitted Workflows may include draft/submitted Tasks to support iterative authoring.

If a task or workflow record is wrong, every derived learning format
becomes wrong. Tasks and workflows are not internal documentation. They
are the foundation upon which all learning experiences are built.

## Core Entities

### Task

A Task represents one atomic outcome.

It defines the procedure required to complete a single step sequence.

- A task produces one outcome.

- The procedure is composed of ordered Steps.

- Each step is written in imperative form.

- A task is self‑contained — all execution detail is defined in its
  record.

- Tasks are reusable components that can be referenced by multiple
  workflows.

Steps must satisfy **Procedure Step Atomicity** rules (see MVP
Definitions).

### Workflow

A Workflow represents a composite outcome made of multiple tasks.

It defines how tasks combine to produce a larger result.

- A workflow consists of ordered Task references, not procedural steps.

- Each task listed is required to produce the workflow objective.

- Workflows link directly to the relevant tasks for execution detail.

- The workflow objective is defined by the organization, not by content
  writers.

- Task order is strict.

Workflows MUST NOT define prerequisites or learning-sequence
requirements. All executable preconditions MUST be declared as Task
Dependencies at the smallest boundary where they are universally true.
Knowledge requirements live inside Tasks as Facts and Concepts. Any
“before starting this workflow” guidance is non-canonical and may be
derived at delivery time from Task Dependencies.

This rule exists because Task Dependencies are the single canonical
location for executable preconditions. Introducing workflow-level
prerequisites would duplicate those constraints, create redundancy, and
allow contradictions to emerge over time.

A Workflow may reference ONLY confirmed Task versions. Unconfirmed or
draft Task versions are not eligible for inclusion because they have not
passed human semantic review and cannot be treated as authoritative.

### Data Schema Relationships

| Entity | Contains | Term Used | Description |
|----|----|----|----|
| Task | Procedure name + Steps table | Steps | Ordered atomic instructions needed to perform one outcome |
| Workflow | Tasks | Tasks | Ordered sequence of tasks that together produce a single workflow objective |

### Required Fields

Each entity must define the following data fields:

#### Task

1.  Title – clear learner‑facing name

2.  Outcome – description of the result of the task

3.  Facts – literal information required to execute the task

4.  Concepts – minimal mental models required to execute the task

5.  Procedure Name – name of the step sequence

6.  Dependencies – required prior knowledge, skills, or conditions

7.  Irreversible flag – required if the task cannot be undone

8.  Task Assets – optional list of asset objects (url, type, label)

#### Workflow

1.  Title – clear learner‑facing name

2.  Objective – organization-defined outcome for the workflow

3.  Tasks – ordered list of task references

## MVP Data Model Notes

### Record Lifecycle Fields (shared by Task and Workflow)

- record_id

- version

- status (draft, submitted, confirmed, deprecated)

- created_at, updated_at

- created_by, updated_by

- reviewed_by, reviewed_at

- change_note

- needs_review_flag

- needs_review_note

### Confirmation Rule

Confirmation Rule

A record may only transition to confirmed through the SME Review action.

Confirmation requires reviewed_by and reviewed_at.

Imports cannot set confirmed status. All imported records are draft or submitted, never confirmed.

A record without reviewed_by and reviewed_at is not confirmed, full stop.

This is consistent with the lifecycle fields you already require.

Confirmed records are immutable. Edits create a new version starting in
Submitted.

### Audit Log (MVP)

All record changes and approvals are logged. Each audit entry records:

- record_id and version

- field changed (or operation type)

- old value and new value (or summary)

- actor (user id)

- timestamp

### Task Storage

- Facts and Concepts are stored as arrays inside the Task record.

- Procedure Name is a string field.

- Steps are stored in a separate table linked to the Task version.

- Task Assets are stored as an array of objects (url, type, label).

### Step Storage (normalized table)

Each Step is linked to a specific Task version. Step IDs are version
scoped.

Required:

- step_text

Optional:

- asset_urls (list)

- ui_hint (plain text, 600 char max)

- additional_info (plain text, 600 char max)

### Workflow Export

Only Workflows are exportable. Single‑task workflows are valid and are
the smallest exportable learning object.

Export formats: Markdown, HTML, PDF, and raw data (includes asset links
and version/status metadata).

## MVP Definitions (Lockdown)

### JSON Import Schema (Summary)

The system accepts validated JSON for Tasks and Workflows. JSON maps
directly to the fields in this document.

Steps are included as an array in the JSON payload and are persisted
into the Task version as `steps_json` (MVP implementation).

Minimum JSON structure:

- Task: title, outcome, facts[], concepts[], procedure_name, steps[],
  dependencies[], irreversible_flag, task_assets[]

- Workflow: title, objective, tasks[]

### Steps Storage (MVP)

In the MVP implementation, Steps are stored inline on the Task version
record as JSON (`tasks.steps_json`).

Each Step is an object:

- `text` (required): what you are doing (intent)
- `actions` (optional): how to do it (tool/environment specific sub-steps)
- `completion` (required): how you prove the Step is complete

Notes:

- There is no separate Steps table in the MVP.
- Optional fields like UI hints or additional step metadata are deferred.

### Task Assets (MVP)

Task assets are stored as an array of objects:

- url

- type (image, video, audio, module, link)

- label

### Status Transitions (MVP)

- Draft → Submitted → Confirmed → Deprecated

- Confirmed records are immutable; edits create a new version in
  Submitted.

### Validation Behavior (MVP)

- Validation produces warnings only.

- Missing required fields block submission.

### Manual Review Flag

- Authors can set needs_review_flag with an optional note to request
  review or indicate suspected outdated content.

### Active Review Definition

#### A. Needs Review flag (record/version)

Fields:

- needs_review_flag: true|false

- needs_review_note: string

- needs_review_at (optional)

- needs_review_by (optional)

Meaning:

This record version may be outdated relative to upstream reality.

Rules:

- Can be set by authors, reviewers, or automated monitors.

- Does not change the status (a confirmed record can be flagged).

- Must be visible in UI anywhere the record appears.

#### B. Update awaiting confirmation (derived UI indicator)

Meaning:

There exists a newer version that is not yet confirmed.

Derived UI signal: update_pending_confirmation

True when:

- Current version is confirmed, and

- There exists a later version with status submitted (or draft if allowed) for the same record_id.

This provides the warning without changing the definition of confirmation.

### Style Rules

- Direct sentences

- Imperative verbs

- No figurative language

- Consistent terminology

- Each step must describe a single, clear operation

### Procedure Step Atomicity

An atomic Step MUST meet all of the following:

- The Step MUST contain one primary action only.

- The Step MUST name an explicit object or target (file, command,
  config key, UI control, etc.).

- The Step MUST include an observable completion condition (what
  indicates the step is complete).

- The Step MUST NOT hide tool choice.
  - The Step may specify a concrete method in `text`, or
  - Provide concrete method guidance in `actions`.

Why this exists: atomic steps are executable, reviewable, and
unambiguous.

### Tool Specificity Policy

- Canonical Task Procedures MUST allow tool-class specificity (e.g., “a
  text editor”) to remain environment-agnostic.

- Derived or beginner views SHOULD provide concrete tool examples (e.g.,
  nano or vi) without altering the canonical Step text.

- Environment profiles (future) SHOULD define default tool choices.

Why this exists: tool choice varies by environment, but procedure intent
must remain stable.

### Example: fstab Step Atomicity

**Bad:** Edit /etc/fstab.

**Good (decomposed Steps):**

1.  Open /etc/fstab in a text editor with elevated permissions.

2.  Insert a new mount entry line containing UUID, mount point,
    filesystem type, and mount options.

3.  Save the file.

4.  Exit the editor.

5.  Run `mount -a` and confirm it exits successfully.

Why this exists: the example demonstrates single-action steps with
explicit method and completion checks.

### Rich Media

Screenshots and other rich media are linked media assets used only when
text cannot be made unambiguous. They are referenced at the step
level, not stored inline and are to be stored as linked URLs.
Assets use external URLs in MVP. Supported types include graphics,
video, audio, Storylane modules, Rise modules, and hosted video
platforms.
Tasks may also reference assets at the task level.

### Error Prevention

- Tasks that include irreversible changes must be flagged at the task
  level.

- Troubleshooting content is not permitted within task or workflow
  steps.

### Quality Gate

Automated validation checks structure and terminology. In MVP, all
validation findings are warnings; human review confirms correctness.

- Workflow: tasks only (no steps)

- Task: steps only (no tasks)

- Imperative language is enforced at the step level

- Terminology consistency is verified across all entities

- Circular dependencies and self-references are blocked

### Structure

Tasks and Workflows together form the hierarchical learning content
graph.

- Tasks are atomic, reusable units.

- Workflows are ordered compositions of tasks.

- All learner‑facing materials are assembled dynamically from this data.

This structure enables automation, version control, advanced analytics,
and AI‑driven content generation, while preserving the clear,
authoritative standards that underpin all learning experiences.

# Database Model

## Atomic Ownership of Knowledge and Procedure

Each Task record is self‑contained and owns its own knowledge, facts,
concepts, and procedural data.

Facts and Concepts are stored as arrays within the Task record. Steps
are stored in a separate table linked to the Task version. These
elements are not shared between Tasks to preserve canonical accuracy.
The Workflow Database provides structure and sequencing without
redefining procedure.

Assessment, Governance, and Delivery databases are defined conceptually
but excluded from MVP scope. These will be introduced in later phases
once core content integrity and authoring workflows are stable.

<table style="width:100%;">
<colgroup>
<col style="width: 23%" />
<col style="width: 41%" />
<col style="width: 35%" />
</colgroup>
<thead>
<tr>
<th>Database</th>
<th>Purpose</th>
<th>Notes</th>
</tr>
</thead>
<tbody>
<tr>
<td>Task (Master) DB</td>
<td>Canonical source of all atomic Task records. Each record defines one
outcome and the full procedure required to achieve it.</td>
<td><ul>
<li><p>Core of the architecture.</p></li>
<li><p>Contains <strong>Facts</strong> and <strong>Concepts</strong> as
arrays, plus a <strong>Procedure Name</strong> field.</p></li>
<li><p>Steps are stored in a separate table linked to the Task
version.</p></li>
<li><p>These elements are <strong>not shared</strong> between Tasks;
each Task owns its own knowledge and procedure.</p></li>
</ul></td>
</tr>
<tr>
<td>Workflow DB</td>
<td>Defines named workflows as ordered sequences of Task IDs. Provides
structure for composite objectives.</td>
<td><ul>
<li><p>References Tasks by ID only; contains <strong>no procedural
detail</strong>.</p></li>
<li><p>Enables composite learning paths without duplicating Task
data.</p></li>
<li><p>Workflows contain tasks only.</p></li>
</ul></td>
</tr>
<tr>
<td>Asset DB</td>
<td>Stores or references rich media (screenshots, diagrams, videos) used
within Tasks or Workflows.</td>
<td><ul>
<li><p>Media stored as linked URLs or file IDs (not inline).</p></li>
<li><p>Tasks and Workflows reference assets by ID.</p></li>
<li><p>External URLs only in MVP.</p></li>
<li><p>Localization and asset versioning are future capabilities.</p></li>
</ul></td>
</tr>
<tr>
<td>Assessment DB</td>
<td>Defines internal learning checks, questions, and rubrics for
non‑certified learning.</td>
<td><ul>
<li><p><strong>Planned for future implementation (not
MVP).</strong></p></li>
<li><p>Certification data remains external.</p></li>
<li><p>Will link to Tasks and Workflows once internal assessment
features are required.</p></li>
<li><p>Future intent: build question banks using database content as
source data for AI.</p></li>
</ul></td>
</tr>
<tr>
<td>Governance / Validation DB</td>
<td>Houses quality rules, style guides, ownership metadata, and version
control.</td>
<td><ul>
<li><p><strong>Planned for future implementation (not
MVP).</strong></p></li>
<li><p>Will manage automated validation such as imperative‑language
checks, terminology checks, and version history.</p></li>
</ul></td>
</tr>
<tr>
<td>Delivery / Publishing DB</td>
<td>Assembles learner‑facing outputs (written guides, videos, SCORM,
etc.) from canonical data.</td>
<td><ul>
<li><p><strong>Planned for future implementation (not
MVP).</strong></p></li>
<li><p>Pulls from Task, Workflow, and Asset DBs.</p></li>
<li><p>Supports localization, rendering, and publication
tracking (future).</p></li>
</ul></td>
</tr>
</tbody>
</table>

# Schema Definition

## Purpose

This section defines the schema for database‑defined learning content.
Each database represents a distinct domain of governance, collectively
forming the Learning Content Ecosystem. All domains operate on shared
principles of accuracy, hierarchy, and traceability. Learning content is
managed as structured data rather than written documents.

## Databases

| Database | Purpose | Core Entities |
|----|----|----|
| Task (Master) DB | Canonical source of all atomic Task records. Each record defines one outcome and the full procedure required to achieve it. | Task, Step, TaskAsset, Dependency |
| Workflow DB | Defines named workflows as ordered sequences of Task IDs. Establishes how Tasks combine to produce composite objectives. | Workflow, TaskReference |
| Asset DB | Stores or references rich media assets (screenshots, diagrams, videos) used within Tasks or Workflows. | Asset, MediaType, UsageContext, Locale, Version |
| Assessment DB | Defines internal learning checks and evaluations for non‑certified learning. | Assessment, Question, Rubric, Result |
| Governance / Validation DB | Houses quality rules, style guides, ownership metadata, and version control. | StyleRule, QualityRule, Owner, Version |
| Delivery / Publishing DB | Assembles learner‑facing outputs (written guides, videos, SCORM, etc.) from canonical data. | GuideInstance, Format, Locale, VersionHistory |

External certification systems remain outside this model. They may
reference learning content through metadata only.

**MVP Note:**  
The **Assessment**, **Governance**, and **Delivery** databases are
defined conceptually but excluded from MVP scope. They will be
implemented in later phases once core content integrity and authoring
workflows are stable.

## Database Separation

The Learning Content Ecosystem separates atomic and composite
definitions:

- The Task (Master) Database contains all atomic Task records. Each
  record is self‑contained and owns its own knowledge, facts,
  concepts, and procedural data. These components are not shared between
  Tasks, ensuring accuracy.

- The Workflow Database defines named workflows as ordered sequences of
  Task IDs. Workflows provide structure and context but contain no
  procedural detail.

This separation ensures Tasks remain reusable and authoritative, while
Workflows provide compositional flexibility.

## Structural Relationships

1.  **Tasks are atomic and self‑contained.**  
    Each Task defines its own facts, concepts, procedure, and outcome.

2.  **Workflows reference Tasks.  **
    A Workflow record contains ordered Task IDs.  
    Workflows contain tasks only.

3.  **Assets support Tasks and Workflows.  **
    Tasks and Workflows link to media assets by ID for clarity and
    reinforcement.

4.  **Governance rules apply globally.  **
    Quality gates, style rules, and validation logic are applied across
    all databases.

5.  **Delivery draws from master data.  **
    Learner‑facing materials are dynamically assembled from Task and
    Workflow data.

## Validation and Quality Governance

- Imperative Language Enforcement: Each Step within a Task must use
  imperative verbs.

- Terminology Consistency: All entities follow defined terminology from
  the governance framework.

- Structural Integrity: Tasks contain only Steps. Workflows contain
  only Tasks (no direct Steps and no prerequisites). Workflows may
  reference only confirmed Task versions. Circular dependencies and
  self-references are blocked.

- Error Prevention: Irreversible tasks are flagged at the task level.
  Troubleshooting content is excluded from steps.

- Validation Split: Structural and terminology checks are automated;
  correctness is confirmed by human review.

### Heuristic Validation Rules for Steps (MVP)

Canonical location: see **Heuristic Validation Rules for Steps (MVP)** in
[Learning Content Governance](Learning_Content_Governance.md).

(This document intentionally does not duplicate the rule text to avoid
cross-doc drift.)

## Data Hierarchy Diagram (Conceptual)

Task (Facts, Concepts, Procedure/Steps) → Workflow (Objective) → Delivery  
Assets ↔ Tasks/Workflows  
Governance applies across all records

This hierarchy represents both data lineage and learning dependency.
Tasks define what must be done. Workflows define how tasks combine to
achieve an objective. Delivery assembles learner-facing outputs from
canonical Task and Workflow data. Assets support clarity at the Task or
Workflow level. Governance ensures integrity across the system.
Assessment is a future layer and is out of scope for MVP.

## Extensibility

The schema is designed for modularity. New databases or entities may be
introduced as learning domains evolve, provided they conform to
governance principles: Canonical data derives from defined sources;
Relationships are explicit and validated; Terminology remains consistent
with the Learning Content Governance standard.

## Summary

The database-defined schema transforms learning content into a
structured, verifiable ecosystem. Tasks and Workflows no longer exist as
isolated written guides; they are interrelated data objects governed by
shared standards. This structure supports automation, maintenance,
analytics, and AI-assisted content generation—all while preserving the
clarity and intent of the original governance framework.
