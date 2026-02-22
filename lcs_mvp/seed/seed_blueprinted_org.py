#!/usr/bin/env python3
"""Seed a large, deterministic demo dataset for the blueprinted_org profile.

Examples:
  python3 seed/seed_blueprinted_org.py --profile blueprinted_org --plan
  python3 seed/seed_blueprinted_org.py --profile blueprinted_org --reset --scale medium --seed 42 --yes
  python3 seed/seed_blueprinted_org.py --profile blueprinted_org --reset --tasks 1800 --workflows 520 --assessments 1100 --seed 1337 --yes
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import (  # type: ignore
    _db_path_for_key,
    _seed_demo_entitlements,
    _seed_demo_users,
    init_db_path,
    utc_now_iso,
)

SEED_NOTE = "seed_blueprinted_org_v1"
ACTOR = "seed"

DOMAINS = [
    "debian",
    "arch",
    "kubernetes",
    "aws",
    "postgres",
    "windows",
    "azure",
    "gcp",
    "terraform",
    "ansible",
    "vmware",
]

WORKFLOW_TAGS = [
    "security",
    "networking",
    "observability",
    "identity",
    "backup",
    "patching",
    "compliance",
    "access-control",
    "incident-response",
    "cost-optimization",
    "resilience",
    "performance",
]

STATUS_PROFILES = {
    "task": {"confirmed": 0.55, "draft": 0.20, "submitted": 0.15, "returned": 0.10},
    "workflow": {"confirmed": 0.50, "draft": 0.20, "submitted": 0.20, "returned": 0.10},
    "assessment": {"confirmed": 0.60, "draft": 0.15, "submitted": 0.15, "returned": 0.10},
}

SCALE_PRESETS = {
    "small": (250, 80, 180),
    "medium": (900, 280, 650),
    "large": (1600, 480, 1000),
}

CANONICAL_TASK_LIBRARIES = {
    "aws": ROOT / "seed" / "canonical_tasks_aws.json",
    "kubernetes": ROOT / "seed" / "canonical_tasks_kubernetes.json",
    "windows": ROOT / "seed" / "canonical_tasks_windows.json",
    "postgres": ROOT / "seed" / "canonical_tasks_postgres.json",
    "arch": ROOT / "seed" / "canonical_tasks_arch.json",
}

CANONICAL_WORKFLOW_LIBRARIES = {
    "aws": ROOT / "seed" / "canonical_workflows_aws.json",
}


@dataclass
class Counts:
    tasks: int
    workflows: int
    assessments: int


def pick_status(rng: random.Random, profile: dict[str, float]) -> str:
    x = rng.random()
    acc = 0.0
    for k, w in profile.items():
        acc += w
        if x <= acc:
            return k
    return list(profile.keys())[-1]


def j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def load_canonical_tasks(domain: str) -> list[dict[str, Any]]:
    p = CANONICAL_TASK_LIBRARIES.get(domain)
    if not p or not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    except Exception:
        return []
    return []


def load_canonical_workflows(domain: str) -> list[dict[str, Any]]:
    p = CANONICAL_WORKFLOW_LIBRARIES.get(domain)
    if not p or not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    except Exception:
        return []
    return []


def make_task_variant(base: dict[str, Any], idx: int, rng: random.Random) -> dict[str, Any]:
    suffixes = [
        "(prod)",
        "(staging)",
        "(shared services)",
        "(regional)",
        "(high-availability)",
        "(cost-optimized)",
        "(hardened)",
        "(standard)",
    ]
    title = str(base.get("title") or "task").strip()
    if rng.random() < 0.75:
        title = f"{title} {rng.choice(suffixes)}"

    return {
        "title": title,
        "outcome": str(base.get("outcome") or "Operational objective achieved."),
        "procedure_name": str(base.get("procedure_name") or "standard-operating-procedure"),
        "facts": [str(x) for x in (base.get("facts") or [])],
        "concepts": [str(x) for x in (base.get("concepts") or [])],
        "dependencies": [str(x) for x in (base.get("dependencies") or [])],
        "steps": list(base.get("steps") or []),
    }


def make_workflow_variant(base: dict[str, Any], idx: int, rng: random.Random) -> dict[str, Any]:
    suffixes = [
        "(production)",
        "(staging)",
        "(shared services)",
        "(regional)",
        "(resilience)",
    ]
    title = str(base.get("title") or "workflow").strip()
    objective = str(base.get("objective") or "Deliver domain operational objective.").strip()
    if rng.random() < 0.65:
        title = f"{title} {rng.choice(suffixes)}"
    tags = [str(x).strip().lower() for x in (base.get("tags") or []) if str(x).strip()]
    if not tags:
        tags = rng.sample(WORKFLOW_TAGS, k=rng.randint(1, 3))
    return {"title": title, "objective": objective, "tags": tags[:3]}


def ensure_domains(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    conn.execute("DELETE FROM user_domains")
    conn.execute("DELETE FROM domains")
    for d in DOMAINS:
        conn.execute(
            "INSERT INTO domains(name, created_at, created_by) VALUES (?,?,?)",
            (d, now, ACTOR),
        )
    _seed_demo_entitlements(conn)


def reset_content(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM workflow_task_refs")
    conn.execute("DELETE FROM assessment_refs")
    conn.execute("DELETE FROM tasks")
    conn.execute("DELETE FROM workflows")
    conn.execute("DELETE FROM assessment_items")
    conn.execute("DELETE FROM export_artifacts")
    conn.execute("DELETE FROM audit_log")
    # keep users/sessions; clear domain registry and re-seed canonical domains
    ensure_domains(conn)


def domain_weights(profile: str) -> list[float]:
    # creates varied pressure. later profiles can tune harder.
    base = [1.0] * len(DOMAINS)
    if profile == "high":
        for i in (0, 2, 5):
            base[i] = 2.0
    elif profile == "spiky":
        for i in (2, 3):
            base[i] = 3.0
    return base


def choose_domain(rng: random.Random, profile: str = "balanced") -> str:
    return rng.choices(DOMAINS, weights=domain_weights(profile), k=1)[0]


def seed_tasks(conn: sqlite3.Connection, rng: random.Random, n: int, pressure_profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = utc_now_iso()

    canonical_by_domain: dict[str, list[dict[str, Any]]] = {
        d: load_canonical_tasks(d) for d in DOMAINS
    }

    # Track used titles per domain to enforce uniqueness
    used_titles_by_domain: dict[str, set[str]] = {d: set() for d in DOMAINS}

    for i in range(1, n + 1):
        rid = f"TSK-{i:06d}"
        domain = choose_domain(rng, pressure_profile)
        status = pick_status(rng, STATUS_PROFILES["task"])

        canonical = canonical_by_domain.get(domain) or []
        if canonical:
            # Cycle through canonicals but ensure unique title within domain
            base = canonical[(i - 1) % len(canonical)]
            variant = make_task_variant(base, i, rng)
            title = variant["title"]
            # Ensure uniqueness by adding index suffix if needed
            original_title = title
            suffix = 1
            while title in used_titles_by_domain[domain]:
                title = f"{original_title} ({suffix})"
                suffix += 1
            used_titles_by_domain[domain].add(title)
            outcome = variant["outcome"]
            procedure_name = variant["procedure_name"]
            facts = variant["facts"]
            concepts = variant["concepts"]
            dependencies = variant["dependencies"]
            steps = variant["steps"]
        else:
            # Fallback generation with uniqueness
            base_title = f"{domain} task {i}"
            title = base_title
            suffix = 1
            while title in used_titles_by_domain[domain]:
                title = f"{base_title} ({suffix})"
                suffix += 1
            used_titles_by_domain[domain].add(title)
            outcome = f"Operational objective for {domain} task {i}"
            procedure_name = "standard-operating-procedure"
            facts = [f"fact {i}", f"domain {domain}"]
            concepts = ["safety", "rollback"]
            dependencies = ["access", "maintenance-window"]
            steps = [
                {
                    "text": "Prepare preconditions",
                    "actions": ["verify state", "capture baseline"],
                    "notes": "Record current values",
                    "completion": "preconditions validated",
                },
                {
                    "text": "Apply change",
                    "actions": ["execute procedure", "verify output"],
                    "notes": "Follow change controls",
                    "completion": "change applied",
                },
            ]

        conn.execute(
            """
            INSERT INTO tasks(
              record_id, version, status, title, outcome,
              facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
              irreversible_flag, task_assets_json, domain, tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rid,
                1,
                status,
                title,
                outcome,
                j(facts),
                j(concepts),
                procedure_name,
                j(steps),
                j(dependencies),
                0,
                j([]),
                domain,
                j([]),  # task tags intentionally empty (workflow-only tags model)
                j({"seed": SEED_NOTE}),
                now,
                now,
                ACTOR,
                ACTOR,
                now if status == "confirmed" else None,
                ACTOR if status == "confirmed" else None,
                "Seeded sample" if status != "draft" else None,
                1 if status in ("submitted", "returned") else 0,
                "awaiting review" if status in ("submitted", "returned") else None,
            ),
        )
        rows.append({"record_id": rid, "version": 1, "status": status, "domain": domain})
    return rows


def seed_workflows(conn: sqlite3.Connection, rng: random.Random, n: int, tasks: list[dict[str, Any]], pressure_profile: str) -> None:
    now = utc_now_iso()

    by_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in DOMAINS}
    for t in tasks:
        by_domain.setdefault(t["domain"], []).append(t)

    canonical_by_domain: dict[str, list[dict[str, Any]]] = {
        d: load_canonical_workflows(d) for d in DOMAINS
    }

    for i in range(1, n + 1):
        rid = f"WF-{i:06d}"
        domain = choose_domain(rng, pressure_profile)
        status = pick_status(rng, STATUS_PROFILES["workflow"])

        pool = by_domain.get(domain) or tasks
        refs_n = rng.randint(3, 8)

        # shape blocked submitted workflows (~25%)
        want_blocked = status == "submitted" and rng.random() < 0.25
        confirmed_pool = [t for t in pool if t["status"] == "confirmed"] or pool
        non_confirmed_pool = [t for t in pool if t["status"] in ("draft", "submitted", "returned")]

        refs = []
        if status == "confirmed":
            refs = rng.sample(confirmed_pool, k=min(refs_n, len(confirmed_pool)))
        elif want_blocked and non_confirmed_pool:
            first = rng.choice(non_confirmed_pool)
            rest_pool = [t for t in confirmed_pool if t["record_id"] != first["record_id"]]
            rest = rng.sample(rest_pool, k=min(max(0, refs_n - 1), len(rest_pool)))
            refs = [first, *rest]
        else:
            refs = rng.sample(pool, k=min(refs_n, len(pool)))

        canonical = canonical_by_domain.get(domain) or []
        if canonical:
            wf_base = canonical[(i - 1) % len(canonical)]
            wf_variant = make_workflow_variant(wf_base, i, rng)
            wf_title = wf_variant["title"]
            wf_objective = wf_variant["objective"]
            tags = wf_variant["tags"]
        else:
            wf_title = f"{domain} workflow {i}"
            wf_objective = f"Deliver {domain} operational objective {i}"
            tags = rng.sample(WORKFLOW_TAGS, k=rng.randint(1, 3))

        conn.execute(
            """
            INSERT INTO workflows(
              record_id, version, status, title, objective,
              domains_json, tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rid,
                1,
                status,
                wf_title,
                wf_objective,
                j([domain]),
                j(tags),
                j({"seed": SEED_NOTE}),
                now,
                now,
                ACTOR,
                ACTOR,
                now if status == "confirmed" else None,
                ACTOR if status == "confirmed" else None,
                "Seeded sample" if status != "draft" else None,
                1 if status in ("submitted", "returned") else 0,
                "awaiting review" if status in ("submitted", "returned") else None,
            ),
        )

        for idx, t in enumerate(refs):
            conn.execute(
                """
                INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                VALUES (?,?,?,?,?)
                """,
                (rid, 1, idx, t["record_id"], t["version"]),
            )


def seed_assessments(conn: sqlite3.Connection, rng: random.Random, n: int, pressure_profile: str) -> None:
    now = utc_now_iso()
    for i in range(1, n + 1):
        rid = f"ASM-{i:06d}"
        domain = choose_domain(rng, pressure_profile)
        status = pick_status(rng, STATUS_PROFILES["assessment"])
        stem = f"Which control best validates {domain} procedure {i}?"

        conn.execute(
            """
            INSERT INTO assessment_items(
              record_id, version, status, stem,
              options_json, correct_key, rationale,
              claim, domains_json, lint_json, refs_json,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rid,
                1,
                status,
                stem,
                j({"A": "baseline", "B": "validation", "C": "rollback", "D": "monitor"}),
                "B",
                "Validation best confirms intended state.",
                "fact_probe",
                j([domain]),
                j([]),
                j([]),
                j([]),
                j({"seed": SEED_NOTE}),
                now,
                now,
                ACTOR,
                ACTOR,
                now if status == "confirmed" else None,
                ACTOR if status == "confirmed" else None,
                "Seeded sample" if status != "draft" else None,
                1 if status in ("submitted", "returned") else 0,
                "awaiting review" if status in ("submitted", "returned") else None,
            ),
        )


def summarize(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["tasks"] = dict(conn.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall())
    out["workflows"] = dict(conn.execute("SELECT status, COUNT(*) c FROM workflows GROUP BY status").fetchall())
    out["assessments"] = dict(conn.execute("SELECT status, COUNT(*) c FROM assessment_items GROUP BY status").fetchall())
    out["domains"] = [r[0] for r in conn.execute("SELECT name FROM domains WHERE disabled_at IS NULL ORDER BY name").fetchall()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="blueprinted_org")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--yes", action="store_true")

    ap.add_argument("--scale", choices=list(SCALE_PRESETS.keys()), default="large")
    ap.add_argument("--tasks", type=int)
    ap.add_argument("--workflows", type=int)
    ap.add_argument("--assessments", type=int)

    ap.add_argument("--pressure-profile", choices=["balanced", "high", "spiky"], default="balanced")
    args = ap.parse_args()

    base = Counts(*SCALE_PRESETS[args.scale])
    counts = Counts(
        tasks=args.tasks if args.tasks is not None else base.tasks,
        workflows=args.workflows if args.workflows is not None else base.workflows,
        assessments=args.assessments if args.assessments is not None else base.assessments,
    )

    print(f"profile={args.profile} seed={args.seed} scale={args.scale} pressure={args.pressure_profile}")
    print(f"counts tasks={counts.tasks} workflows={counts.workflows} assessments={counts.assessments}")

    if args.plan:
        print("Plan only: no DB writes.")
        return

    if args.reset and not args.yes:
        raise SystemExit("Refusing reset without --yes")

    db_path = _db_path_for_key(args.profile)
    init_db_path(db_path)

    rng = random.Random(args.seed)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            _seed_demo_users(conn)
            if args.reset:
                reset_content(conn)
            else:
                ensure_domains(conn)

            tasks = seed_tasks(conn, rng, counts.tasks, args.pressure_profile)
            seed_workflows(conn, rng, counts.workflows, tasks, args.pressure_profile)
            seed_assessments(conn, rng, counts.assessments, args.pressure_profile)

            conn.execute(
                "INSERT INTO audit_log(entity_type, record_id, version, action, actor, at, note) VALUES (?,?,?,?,?,?,?)",
                ("seed", args.profile, 1, "seed_blueprinted_org", ACTOR, utc_now_iso(), SEED_NOTE),
            )

        print("Seed complete:")
        print(json.dumps(summarize(conn), indent=2))
        print(f"db_path={db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
