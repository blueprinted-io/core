# Auth + Domains (Draft)

Status: **draft**

This document captures an initial direction for authentication/authorization and domain-scoped governance.
It is intentionally conservative: it aims to preserve the semantics of confirmation while enabling enterprise-style ownership and accountability.

---

## Terminology

- **Domain**: A **product-led** scope boundary. Examples: `linux`, `kubernetes`, `postgres`, `aws`.
  - Domains are **not** conceptual categories like “security” or “deployment”.
  - Domains exist to determine **who is authorized to confirm** a record.

- **Tags**: Conceptual labels used for discovery and filtering. Examples: `security`, `deployment`, `storage`.
  - Tags do **not** confer authority.

- **Task**: An atomic, outcome-driven unit of work.

- **Workflow**: An ordered composition of tasks that produces one objective.

---

## Core principle

> Confirmation is an authority boundary.

- Confirming a **Task** means: the task definition is correct and executable in its domain.
- Confirming a **Workflow** means: the composition of the referenced tasks produces the stated objective in reality.

This implies that confirmation must be gated by explicit authorization rules.

---

## Domain rules

### Tasks

- A Task has **exactly one** domain.
- Domain is **required** for:
  - `submitted`
  - `confirmed`
- Domain may be **empty** only in `draft`.

Rationale:
- Tasks remain cleanly reviewable by a single domain SME.
- Drafts can be authored before domain assignment is finalized.

### Workflows

- A Workflow is **multi-domain** by definition.
- Workflow domains are **derived**, not manually authored:

```
workflow.domains = UNION(task.domain for each referenced Task version)
```

Rationale:
- Prevents drift/loopholes where a workflow “forgets” to declare a domain.
- Makes workflow governance mechanically consistent with task composition.

---

## Confirmation authorization rules

### Confirming tasks

A reviewer may confirm a Task only if they are authorized for that Task’s domain.

### Confirming workflows

A reviewer may confirm a Workflow only if:

1) all referenced Task versions are confirmed, and
2) the reviewer is authorized for **every domain** in the workflow’s derived domain set.

Rationale:
- Confirming a workflow is an integration claim.
- If the organization cannot produce a single reviewer who holds all relevant domains, it should not publish the workflow.

---

## Admin override (break-glass)

An admin override is permitted as a continuity mechanism (e.g. organizational turnover), but it must be treated as a controlled breach:

- Any admin “force confirm” must be explicitly recorded as an override.
- An override should require a reason.
- Overrides should be visible as a banner/scar in the UI and exports.

Rationale:
- Prevents the system from becoming unusable.
- Preserves the integrity of normal confirmation as a trust boundary.

---

## User management & groups

### MVP direction

- User management is **admin-only**.
- Authorization is via **group mapping**:
  - Users belong to groups.
  - Groups confer domain confirmation rights.

Notes:
- In MVP, “groups” may be local to the app.
- Later, groups may be mapped from an external IdP (Okta/Azure AD) via OIDC claims.

---

## Open questions (to revisit)

1) **Domain registry**: should domains be free-text or centrally registered?
2) **Unassigned domain propagation**: should workflows be blocked from submission if any referenced task has an empty domain?
3) **Override policy**: should overrides be time-bound or automatically flag records for review?
4) **UI requirements**:
   - Show required workflow domains.
   - Show reviewer’s authorized domains.
   - Show missing domains (delta) when confirmation is blocked.

---

## Non-goals (for this draft)

- Full authentication (SSO/OIDC) implementation.
- Fine-grained per-user exceptions.
- Multi-party workflow confirmations.

This is a first pass, meant to be read, challenged, and refined before implementation.

---

## JSON API auth pattern (agent/programmatic access)

The `/api/*` routes expose a JSON REST layer over the same governance model. They use
the **same session cookie** as the browser UI — no separate API key or token system exists.

### Flow

1. **Log in** — POST to the existing `/login` endpoint with form-encoded credentials:

   ```
   POST /login
   Content-Type: application/x-www-form-urlencoded

   username=agent-user&password=secret&db_key=debian
   ```

   On success the server sets a `lcs_session` cookie (HTTP-only). The response is a
   redirect (303); follow it or simply retain the cookie.

2. **Call API endpoints** — include the `lcs_session` cookie on all subsequent requests:

   ```
   GET /api/tasks?status=confirmed
   Cookie: lcs_session=<token>
   ```

3. **All RBAC rules apply** — the role attached to the session determines what actions
   are permitted (same matrix as the HTML routes). A `403` response means the session
   role lacks the required permission; a `401` means the session is missing or expired.

### Error format

All `/api/*` error responses are JSON:

```json
{ "detail": "<human-readable message>" }
```

HTTP status codes follow standard semantics: 400 bad request, 401 unauthenticated,
403 forbidden, 404 not found, 409 state conflict.

### Notes

- Sessions are scoped to a database profile (set via the `db_key` form field at login,
  or the `lcs_db` cookie). Agents operating on a specific database should log in with
  the matching `db_key`.
- The `/api/db/state` endpoint provides a full state snapshot in one call — useful for
  an agent to orient itself before issuing writes.
- Domain entitlement checks on submit/confirm/return are identical to those enforced
  by the HTML routes. An agent user must hold the relevant domain to act on records in
  that domain.
