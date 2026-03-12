# README — Learning Theory Rationale

## Purpose of This Document

This document explains the learning-theory rationale behind the Task and Workflow model.

It exists to answer a specific concern:

> “This looks highly procedural. Is that actually good learning design?”

The answer is yes, provided the system is understood for what it is and what it is not.

This is not a general theory of learning.  
It is a justification for a **bounded, execution-focused learning system** designed to support real-world performance in technical and systems-based domains.

For the system data model and schema, see [Learning Content System Design](docs/governance_and_design/Learning_Content_System_Design.md).  
For standards, governance, and review rules, see [Learning Content Governance](docs/governance_and_design/Learning_Content_Governance.md).

---

## The Central Position

This system is grounded in a deliberately narrow premise:

**At this level, the purpose of learning is not mastery.  
The purpose of learning is to create the conditions under which mastery can later develop.**

The system therefore optimizes for:

- correctness  
- repeatability  
- clarity  
- transfer to real work  

It explicitly does not attempt to encode higher-order expertise, diagnostic reasoning, or creative mastery.

That boundary is intentional.

---

## Relationship to Bloom, Krathwohl, and Cognitive Domains

Bloom’s taxonomy and its later revisions are often treated as instructional ladders, implying that learning design should progressively “move learners upward” toward higher-order cognition.

This system rejects that interpretation.

Bloom describes **types of cognitive activity**, not guarantees of how those activities can be produced, taught, or assessed.

When applied to real-world, high-stakes work, Bloom’s taxonomy is structurally incomplete unless an additional dimension is made explicit.

---

## A Structural Reinterpretation of Bloom’s Taxonomy

Rather than a ladder, Bloom can be understood as a **two-dimensional structure**.

| Row Context | Single Element | Multiple Elements | Action / Output |
| --- | --- | --- | --- |
| Situated Reality | Analyze — Examine a concept or system element in context | Evaluate — Compare and judge competing models | Create — Produce novel output under real constraints |
| Training Utopia | Remember — Recall a fact or definition | Understand — Relate facts and concepts correctly | Apply — Execute a known procedure to produce a defined outcome |

### The Rows: Where Cognition Can Occur

The lower row represents cognitive operations that can be **directly taught, practiced, and assessed** in controlled environments.

These operate reliably in what might be called a *training utopia*: low ambiguity, minimal consequence, and clearly defined correctness.

The upper row represents cognitive operations that **cannot be reliably taught in isolation**. They require real constraints, meaningful consequences, incomplete information, and time pressure.

These capabilities emerge only through situated experience in the real world.

---

### The Columns: What Kind of Cognitive Object Is Involved

The columns describe the type of cognitive object being acted upon:

- Column 1: single elements such as facts or isolated concepts  
- Column 2: relationships between multiple elements  
- Column 3: actionable output  

In organizational learning contexts, the third column is the purpose.

Remembering, understanding, analyzing, and evaluating exist in service of producing correct or novel action under constraints.

---

## On Higher-Order Cognitive Skills

This model takes a specific position on higher-order cognition.

Higher-order cognitive activities such as analysis, evaluation, and creation **cannot be taught directly in and of themselves**.

What can be taught are the supporting skills: logical reasoning, comparison, abstraction, and reflection. These skills only become meaningful when applied to a domain where the learner already has reliable executional competence.

Analysis without accurate application is speculation.  
Evaluation without procedural grounding is opinion.  
Creation without mastery of constraints is improvisation.

Higher-order cognition is therefore treated as **emergent**, not authorable.

---

## Why Procedural Focus Is Not a Flaw

Procedural learning is often dismissed as shallow or mechanical.

In high-stakes domains, this is incorrect.

Consider aviation.

A commercial pilot operates in an environment where failure carries catastrophic consequences. As a result, flying is highly procedural. Checklists are followed precisely, even by experts with thousands of flight hours.

This is not because pilots lack understanding.  
It is because procedures reduce cognitive load, prevent omission, and protect against error under pressure.

Expertise does not eliminate procedure.  
It increases respect for it.

This system applies the same logic.

Whenever people work with systems, infrastructure, or data, they are operating in environments where mistakes carry real cost. Procedural accuracy is therefore not a beginner crutch. It is a safety mechanism.

---

## Separation of Knowledge Types

Each Task explicitly separates:

- **Facts**: information that must be known  
- **Concepts**: mental models required to understand why steps work  
- **Procedure**: actions required to produce an outcome  

This reflects well-established cognitive findings:

- facts without concepts produce rote behavior  
- concepts without procedure produce abstraction without capability  
- procedure without verification produces superstition  

The system requires all three, without claiming this produces mastery.

The precise field definitions and storage rules for Facts, Concepts, Procedure, and Dependencies are specified in [Learning Content System Design](docs/governance_and_design/Learning_Content_System_Design.md), with usage standards in [Learning Content Governance](docs/governance_and_design/Learning_Content_Governance.md).

---

## Why Tasks Are Atomic

Tasks are defined as atomic units of outcome for structural reasons.

Atomicity ensures:

- clarity  
- verifiability  
- reusability  
- governance  

Tasks are not complete learning experiences.  
They are honest representations of work.

---

## Why Workflows Avoid Learning Sequences

Workflows do not define pedagogical sequencing.

They define **capability dependencies only**.

Learning sequence, pacing, and instructional strategy vary by context and audience. Encoding them as canonical truth creates rigidity and drift.

This system treats sequencing as a delivery concern, not a content truth.

The canonical workflow rules and governance constraints are defined in [Learning Content Governance](docs/governance_and_design/Learning_Content_Governance.md), with structural constraints and schema detail in [Learning Content System Design](docs/governance_and_design/Learning_Content_System_Design.md).

---

## Troubleshooting and Diagnostic Reasoning

Troubleshooting is intentionally excluded from Tasks.

Effective troubleshooting requires:

- domain mastery  
- pattern recognition  
- hypothesis generation  
- tolerance for ambiguity  

These capabilities do not reliably develop through procedural content alone.

This system establishes correct execution first and leaves diagnostic reasoning to experience, practice, and separate interventions.

---

## Alignment with Experiential Learning

This model assumes and depends on experiential learning.

It is designed to support environments where learners:

- practice in labs or sandboxes  
- perform observed or coached tasks  
- receive feedback  
- make and recover from mistakes  

The system provides the **reference point** that makes experience meaningful.

Without a stable definition of correct execution, experience becomes noise.

---

## What This System Explicitly Refuses to Claim

This system does not claim that:

- procedures create mastery  
- higher-order cognition can be authored  
- expertise can be scaled as content  
- assessment can fully validate cognition  

These refusals are not limitations.  
They are acknowledgements of reality.

---

## Summary

This system is grounded in learning theory that prioritizes:

- accurate application  
- explicit mental models  
- experiential development of expertise  

It deliberately constrains its scope to what can be reliably defined, reviewed, and governed.

It is not a complete theory of learning.

It is a **structurally honest system** designed to support learning where it actually happens: in work, practice, and experience.
