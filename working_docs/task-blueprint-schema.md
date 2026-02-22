# Task Blueprint Schema (Phase 2 Hybrid Realism)

Purpose: canonical human-quality task definitions used as source material for seeded variants.

## Required fields

- `domain` (string): operational domain (e.g. `aws`)
- `title` (string): concrete, action-oriented title
- `outcome` (string): observable end state
- `procedure_name` (string): canonical procedure label
- `facts` (string[]): literal prerequisite knowledge
- `concepts` (string[]): minimal mental models required
- `dependencies` (string[]): hard execution prerequisites
- `steps` (object[]): atomic procedure steps
  - `text` (required)
  - `actions` (optional string[])
  - `notes` (optional string)
  - `completion` (required)

## Rules

- No generic placeholders (`task 1`, `workflow 2`, etc.)
- Task remains intent-agnostic and reusable
- Workflow-only tags model remains intact (task tags empty)
- Steps must be atomic and testable

## Variant generation guidance

- Generate 3-8 variants per canonical task
- Vary environment detail and scale context, not core mechanism
- Preserve outcome semantics and verification step
