# AI Import Prompt — Filling the JSON Schema

This document contains a reusable prompt you can paste into any AI assistant (ChatGPT, Claude, Gemini, etc.) alongside a source document — a standard operating procedure, training manual, technical guide, or similar — to produce JSON that imports cleanly into the system.

---

## How to use it

1. Open the prompt below.
2. Paste your source document **after** the line `SOURCE DOCUMENT:`.
3. Send it to an AI assistant.
4. Copy the JSON output and save it as a `.json` file.
5. Upload the file through **Import → Import JSON**.

---

## The Prompt

```
You are converting a source document into structured learning content.
Your output MUST be valid JSON only — no markdown, no commentary, no code fences.

---

## DEFINITIONS

Before you write anything, understand these three content types precisely:

**FACTS** — prerequisite knowledge.
These are things the learner must already know or have in front of them BEFORE starting the task.
Facts are declarative: measurements, tolerances, names, identifiers, regulatory requirements, safety precautions.
Examples:
  - "The isolation valve is located at Panel B3."
  - "This procedure requires a signed permit-to-work."
  - "The rated torque for this fitting is 25 Nm."
Write each fact as a single, complete sentence.

**CONCEPTS** — the why and the science.
These explain what the task is fundamentally trying to achieve and why it exists.
A concept answers: "What principle is at work here? What happens if this is done wrong? Why is this task necessary?"
Examples:
  - "Calibration removes accumulated sensor drift that builds up over time."
  - "Isolating the circuit prevents back-EMF from damaging the replacement component."
  - "The rinse step removes residual reagent which would interfere with the next assay."
Write each concept as one or two sentences. Keep it brief and explanatory, not instructional.

**STEPS** — the procedure itself.
Each step is a single physical or digital action the performer takes.
Rules for steps:
  - One action only per step. Never combine two actions with "and", "then", or "also".
  - Start with a concrete verb: open, close, press, turn, click, run, record, insert, remove, attach, verify.
  - Do NOT start with abstract verbs: configure, manage, set up, ensure, handle, prepare, troubleshoot, edit.
  - Every step MUST include a "completion" — an observable confirmation that the step is done.
    Good completion: "Panel light turns green." / "Terminal shows 'OK'." / "Torque wrench clicks."
    Bad completion: "Step is complete." / "Done."
  - Do not include troubleshooting in steps.

---

## OUTPUT SCHEMA

Return a JSON object with this exact structure:

{
  "tasks": [
    {
      "title": "Short imperative title (max 10 words)",
      "outcome": "One sentence: what is achieved when this task is complete?",
      "procedure_name": "The formal name of the procedure this task belongs to (or repeat the title)",
      "facts": [
        "Fact one.",
        "Fact two."
      ],
      "concepts": [
        "Concept one.",
        "Concept two."
      ],
      "dependencies": [
        "Title of another task that must be completed before this one (leave empty if none)"
      ],
      "steps": [
        {
          "text": "What the performer does (single action, concrete verb)",
          "completion": "Observable confirmation that this step is done",
          "actions": [
            "Optional: sub-instruction for a specific tool or UI, e.g. 'In SAP: navigate to MM60 > Enter plant code'"
          ]
        }
      ]
    }
  ],
  "workflows": [
    {
      "title": "Workflow title",
      "objective": "One sentence: what does completing this sequence of tasks achieve?",
      "task_refs": [
        {"record_id": "__PLACEHOLDER__", "version": 1}
      ]
    }
  ]
}

---

## SCALE CONTROLS

Apply these rules when processing the source document:

1. **Task limit**: Extract a maximum of 10 tasks per response. If the document clearly contains more than 10 tasks, extract the 10 most self-contained and important ones, then add a note at the very end of your JSON output (inside a top-level "import_notes" key) listing the titles of tasks you did not include. The user can then ask you to continue with the remaining tasks in a follow-up.

2. **Workflows**: Only propose a workflow if the source document explicitly describes a sequence or grouping of tasks. Do not invent workflow groupings. If you propose a workflow, set "task_refs" to a list of placeholder objects ({"record_id": "__PLACEHOLDER__", "version": 1}) — one per task in the workflow — so the user knows where to fill in real IDs after import.

3. **Single task documents**: If the source only describes one task, output a single task inside the "tasks" array and omit the "workflows" key entirely.

4. **Ambiguous steps**: If a step is unclear in the source, write what you can infer and add a note in the step's "completion" field starting with "CHECK: " to flag it for human review.

5. **Missing facts or concepts**: If the source does not contain prerequisite knowledge or explanatory content, set "facts" and "concepts" to empty arrays []. Do not invent content.

---

## QUALITY CHECKLIST (self-verify before outputting)

Before producing the JSON, verify:
- [ ] Every task has a non-empty "title", "outcome", and at least one step.
- [ ] Every step has both a non-empty "text" and a non-empty "completion".
- [ ] No step text begins with: configure, manage, set up, ensure, handle, prepare, troubleshoot, edit.
- [ ] No step combines two actions (no "X and then Y" patterns).
- [ ] All steps describe actions that exist in the source document. Do not invent steps.
- [ ] Output is valid JSON with no markdown fences, no commentary.

---

SOURCE DOCUMENT:
[PASTE YOUR DOCUMENT HERE]
```

---

## Field reference (quick cheat sheet)

| Field | Required | Plain English |
|---|---|---|
| `title` | Yes | Short name for the task. Start with a verb. Max ~10 words. |
| `outcome` | Yes | What is achieved when the task is done. One sentence. |
| `procedure_name` | No | The formal procedure this task belongs to. Defaults to title. |
| `facts` | No | Prerequisite knowledge — what you must know before starting. |
| `concepts` | No | The science and reasoning — why this task exists. |
| `dependencies` | No | Other task titles that must be done first. |
| `steps[].text` | Yes | One concrete action. Start with a verb. One action only. |
| `steps[].completion` | Yes | How the performer knows the step is done. Observable. |
| `steps[].actions` | No | Sub-instructions for a specific tool, app, or UI. |
| `workflows[].title` | Yes | Name for the sequence. |
| `workflows[].objective` | Yes | What the sequence as a whole achieves. |
| `workflows[].task_refs` | Yes | List of task references: `{"record_id": "...", "version": 1}` |

---

## Common mistakes to avoid

**Facts that are actually concepts:**
> "The valve must be isolated because open lines create back-pressure." — this is a concept, not a fact. A fact would be: "The isolation valve is at station 4."

**Concepts that are actually instructions:**
> "The operator should check the gauge before proceeding." — this is a step, not a concept. A concept would be: "Pressure build-up before isolation can cause seal failure."

**Steps that bundle two actions:**
> "Open the valve and check the flow rate." — split this into two steps: one to open the valve, one to verify flow.

**Completion checks that are not observable:**
> "The step is complete." — useless. Write what you can actually see, hear, or read: "Gauge reads between 2–4 bar." / "LED turns solid green." / "Command returns exit code 0."
