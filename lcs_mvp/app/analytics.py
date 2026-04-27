from __future__ import annotations

import math
import sqlite3
from typing import Any

from .config import STALENESS_DAYS
from .audit import _normalize_domains
from .database import _workflow_domains, workflow_readiness


# ---------------------------------------------------------------------------
# System health metrics (home dashboard)
# ---------------------------------------------------------------------------

def _system_health_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute system-wide LearningOps health metrics for the home dashboard.

    All metrics are global (not domain-scoped) so every role sees the same
    system health picture regardless of their domain entitlements.
    """
    staleness_param = f"-{STALENESS_DAYS} days"

    # --- Pipeline velocity (last 30 days) ---
    throughput = conn.execute(
        "SELECT COUNT(*) FROM audit_log"
        " WHERE action='confirm'"
        " AND entity_type IN ('task','workflow','assessment')"
        " AND at >= datetime('now','-30 days')"
    ).fetchone()[0]

    cycle_hours = conn.execute(
        "SELECT ROUND(AVG((julianday(c.at)-julianday(s.at))*24),1)"
        " FROM audit_log s"
        " JOIN audit_log c ON c.entity_type=s.entity_type"
        "   AND c.record_id=s.record_id AND c.version=s.version"
        "   AND c.action='confirm'"
        " WHERE s.action='submit' AND s.at >= datetime('now','-30 days')"
    ).fetchone()[0]

    rr = conn.execute(
        "SELECT SUM(CASE WHEN action='return_for_changes' THEN 1 ELSE 0 END),"
        "       SUM(CASE WHEN action='submit' THEN 1 ELSE 0 END)"
        " FROM audit_log"
        " WHERE entity_type IN ('task','workflow','assessment')"
        " AND at >= datetime('now','-30 days')"
    ).fetchone()
    return_rate_pct = round(rr[0] * 100 / rr[1], 1) if (rr and rr[1]) else None

    recovery_hours = conn.execute(
        "SELECT ROUND(AVG((julianday(c.at)-julianday(r.at))*24),1)"
        " FROM audit_log r"
        " JOIN audit_log c ON c.record_id=r.record_id"
        "   AND c.at > r.at AND c.action='confirm'"
        "   AND c.entity_type=r.entity_type"
        " WHERE r.action='return_for_changes'"
        " AND r.at >= datetime('now','-60 days')"
    ).fetchone()[0]

    velocity = {
        "throughput": int(throughput or 0),
        "cycle_hours": cycle_hours,
        "return_rate_pct": return_rate_pct,
        "recovery_hours": recovery_hours,
    }

    # --- Staleness per domain ---
    task_stale = conn.execute(
        "SELECT t.domain, COUNT(*) AS total,"
        "  SUM(CASE WHEN t.reviewed_at IS NULL"
        "       OR t.reviewed_at < datetime('now',?)"
        "       THEN 1 ELSE 0 END) AS stale"
        " FROM (SELECT record_id, MAX(version) AS v FROM tasks GROUP BY record_id) l"
        " JOIN tasks t ON t.record_id=l.record_id AND t.version=l.v"
        " WHERE t.status='confirmed' AND t.domain != ''"
        " GROUP BY t.domain",
        (staleness_param,),
    ).fetchall()

    wf_stale = conn.execute(
        "SELECT LOWER(TRIM(je.value)) AS domain, COUNT(DISTINCT w.record_id) AS total,"
        "  SUM(CASE WHEN w.reviewed_at IS NULL"
        "       OR w.reviewed_at < datetime('now',?)"
        "       THEN 1 ELSE 0 END) AS stale"
        " FROM (SELECT record_id, MAX(version) AS v FROM workflows GROUP BY record_id) l"
        " JOIN workflows w ON w.record_id=l.record_id AND w.version=l.v,"
        " json_each(COALESCE(w.domains_json,'[]')) je"
        " WHERE w.status='confirmed'"
        " GROUP BY domain",
        (staleness_param,),
    ).fetchall()

    stale_by_domain: dict[str, dict[str, int]] = {}
    for row in task_stale:
        d = str(row["domain"]).lower()
        stale_by_domain.setdefault(d, {"domain": str(row["domain"]), "confirmed_tasks": 0, "stale_tasks": 0, "confirmed_workflows": 0, "stale_workflows": 0})
        stale_by_domain[d]["confirmed_tasks"] = int(row["total"])
        stale_by_domain[d]["stale_tasks"] = int(row["stale"])
    for row in wf_stale:
        d = str(row["domain"]).lower()
        stale_by_domain.setdefault(d, {"domain": str(row["domain"]), "confirmed_tasks": 0, "stale_tasks": 0, "confirmed_workflows": 0, "stale_workflows": 0})
        stale_by_domain[d]["confirmed_workflows"] = int(row["total"])
        stale_by_domain[d]["stale_workflows"] = int(row["stale"])

    staleness = sorted(stale_by_domain.values(), key=lambda x: (-(x["stale_tasks"] + x["stale_workflows"]), x["domain"]))

    # --- Assessment coverage per domain ---
    coverage_rows = conn.execute(
        "SELECT t.domain,"
        "  COUNT(DISTINCT t.record_id) AS confirmed_tasks,"
        "  COUNT(DISTINCT ar.ref_record_id) AS covered_tasks"
        " FROM (SELECT record_id, MAX(version) AS v FROM tasks GROUP BY record_id) l"
        " JOIN tasks t ON t.record_id=l.record_id AND t.version=l.v"
        " LEFT JOIN assessment_refs ar"
        "   ON ar.ref_record_id=t.record_id AND ar.ref_type='task'"
        "   AND EXISTS ("
        "     SELECT 1 FROM assessment_items ai"
        "     WHERE ai.record_id=ar.assessment_record_id"
        "     AND ai.version=ar.assessment_version AND ai.status='confirmed'"
        "   )"
        " WHERE t.status='confirmed' AND t.domain != ''"
        " GROUP BY t.domain ORDER BY t.domain"
    ).fetchall()

    coverage = [
        {
            "domain": str(r["domain"]),
            "confirmed_tasks": int(r["confirmed_tasks"]),
            "covered_tasks": int(r["covered_tasks"]),
        }
        for r in coverage_rows
    ]

    return {"velocity": velocity, "staleness": staleness, "coverage": coverage}


# ---------------------------------------------------------------------------
# Visualisation sub-functions
# ---------------------------------------------------------------------------

def _viz_coverage_gaps(system_health: dict[str, Any]) -> dict[str, Any]:
    """Coverage gap ranking from system health data."""
    cov_rows = list(system_health.get("coverage") or [])
    cov_items: list[dict[str, Any]] = []
    total_gap = 0
    for r in cov_rows:
        confirmed = int(r.get("confirmed_tasks") or 0)
        covered = int(r.get("covered_tasks") or 0)
        gap = max(0, confirmed - covered)
        pct = int((covered * 100 // confirmed) if confirmed else 0)
        total_gap += gap
        if pct >= 80:
            sev = "low"
        elif pct >= 50:
            sev = "med"
        elif pct >= 25:
            sev = "high"
        else:
            sev = "critical"
        cov_items.append(
            {
                "domain": str(r.get("domain") or ""),
                "confirmed": confirmed,
                "covered": covered,
                "gap": gap,
                "pct": pct,
                "severity": sev,
            }
        )
    cov_items.sort(key=lambda x: (-int(x["gap"]), str(x["domain"])))
    cov_rank = [x for x in cov_items if int(x["gap"]) > 0]
    cov_rank_max = max([int(x["gap"]) for x in cov_rank] + [1])
    for x in cov_rank:
        x["bar_pct"] = round((float(x["gap"]) * 100.0) / float(cov_rank_max), 1)
    return {"total_gap": int(total_gap), "items": cov_rank}


def _viz_pipeline_flow(
    tasks_status: dict[str, int],
    workflows_status: dict[str, int],
    assessments_status: dict[str, int],
    returned_tasks: int,
    returned_workflows: int,
    returned_assessments: int,
    domain_pressure_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pipeline flow bar chart and return-distribution pie chart."""
    flow_stages = [
        {
            "key": "draft",
            "name": "Draft",
            "value": int(tasks_status.get("draft", 0) + workflows_status.get("draft", 0) + assessments_status.get("draft", 0)),
        },
        {
            "key": "submitted",
            "name": "Submitted",
            "value": int(tasks_status.get("submitted", 0) + workflows_status.get("submitted", 0) + assessments_status.get("submitted", 0)),
        },
        {
            "key": "returned",
            "name": "Returned",
            "value": int(tasks_status.get("returned", 0) + workflows_status.get("returned", 0) + assessments_status.get("returned", 0)),
        },
        {
            "key": "confirmed",
            "name": "Confirmed",
            "value": int(tasks_status.get("confirmed", 0) + workflows_status.get("confirmed", 0) + assessments_status.get("confirmed", 0)),
        },
    ]
    flow_max = max([int(s["value"]) for s in flow_stages] + [1])
    for s in flow_stages:
        s["width_pct"] = round((float(s["value"]) * 100.0) / float(flow_max), 1)
    flow_returns = [
        {"name": "Tasks", "value": int(returned_tasks)},
        {"name": "Workflows", "value": int(returned_workflows)},
        {"name": "Assessments", "value": int(returned_assessments)},
    ]
    return_dist = [
        {"domain": str(d.get("domain") or ""), "value": int(d.get("returned_total") or 0)}
        for d in domain_pressure_rows
    ]
    return_dist.sort(key=lambda x: (-int(x["value"]), str(x["domain"])))
    return_pie_total = int(sum(int(x["value"]) for x in return_dist))
    pie_cx = 120.0
    pie_cy = 120.0
    pie_r = 100.0
    pie_palette = [
        "#dc2626", "#f59e0b", "#2563eb", "#14b8a6", "#7c3aed",
        "#ea580c", "#0ea5e9", "#9333ea", "#16a34a", "#be123c",
    ]
    pie_slices: list[dict[str, Any]] = []
    if return_pie_total > 0:
        pie_source = [x for x in return_dist if int(x["value"]) > 0]
        start_deg = -90.0
        for idx, x in enumerate(pie_source):
            value = int(x["value"])
            sweep = (float(value) / float(return_pie_total)) * 360.0
            end_deg = start_deg + sweep
            start_rad = math.radians(start_deg)
            end_rad = math.radians(end_deg)
            x1 = pie_cx + pie_r * math.cos(start_rad)
            y1 = pie_cy + pie_r * math.sin(start_rad)
            x2 = pie_cx + pie_r * math.cos(end_rad)
            y2 = pie_cy + pie_r * math.sin(end_rad)
            large_arc = 1 if sweep > 180.0 else 0
            path = (
                f"M {pie_cx:.2f} {pie_cy:.2f} "
                f"L {x1:.2f} {y1:.2f} "
                f"A {pie_r:.2f} {pie_r:.2f} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z"
            )
            pie_slices.append(
                {
                    "domain": str(x["domain"]),
                    "value": value,
                    "pct": round((float(value) * 100.0) / float(return_pie_total), 1),
                    "path": path,
                    "color": pie_palette[idx % len(pie_palette)],
                }
            )
            start_deg = end_deg
    pipeline_flow = {"stages": flow_stages, "returns": flow_returns}
    returns_pie = {
        "total": return_pie_total,
        "size": 240,
        "cx": pie_cx,
        "cy": pie_cy,
        "r": pie_r,
        "slices": pie_slices,
    }
    return pipeline_flow, returns_pie


def _viz_cycle_histogram(conn: sqlite3.Connection, trend_days: int) -> dict[str, Any]:
    """Workflow submit-to-confirm cycle-time histogram."""
    cycle_rows = conn.execute(
        """
        SELECT ((julianday(c.at)-julianday(s.at))*24.0) AS h
        FROM audit_log s
        JOIN audit_log c
          ON c.entity_type=s.entity_type
         AND c.record_id=s.record_id
         AND c.version=s.version
         AND c.action='confirm'
        WHERE s.action='submit'
          AND s.entity_type='workflow'
          AND s.at >= datetime('now','-90 days')
        """
    ).fetchall()
    durations = [float(r["h"]) for r in cycle_rows if r and r["h"] is not None and float(r["h"]) >= 0.0]
    if durations:
        cycle_avg = round(sum(durations) / len(durations), 1)
    else:
        cycle_avg = None
    bins: list[tuple[str, float, float | None]] = [
        ("0-1h", 0.0, 1.0),
        ("1-2h", 1.0, 2.0),
        ("2-4h", 2.0, 4.0),
        ("4-8h", 4.0, 8.0),
        ("8-24h", 8.0, 24.0),
        ("24h+", 24.0, None),
    ]
    cycle_hist: list[dict[str, Any]] = []
    for label, lo, hi in bins:
        if hi is None:
            c = sum(1 for d in durations if d >= lo)
        else:
            c = sum(1 for d in durations if d >= lo and d < hi)
        cycle_hist.append({"label": label, "count": int(c)})
    cycle_total = int(sum(int(x["count"]) for x in cycle_hist))
    for x in cycle_hist:
        x["pct_total"] = round((float(x["count"]) * 100.0) / float(cycle_total), 1) if cycle_total else 0.0
    group_defs = [
        ("fast", "Fast (<2h)", ["0-1h", "1-2h"]),
        ("normal", "Nominal (2-8h)", ["2-4h", "4-8h"]),
        ("slow", "Slow (8-24h)", ["8-24h"]),
        ("critical", "Critical (24h+)", ["24h+"]),
    ]
    cycle_groups: list[dict[str, Any]] = []
    counts_by_label = {str(x["label"]): int(x["count"]) for x in cycle_hist}
    for key, label, labels in group_defs:
        c = int(sum(counts_by_label.get(lb, 0) for lb in labels))
        p = round((float(c) * 100.0) / float(cycle_total), 1) if cycle_total else 0.0
        cycle_groups.append({"key": key, "label": label, "count": c, "pct": p})
    if cycle_avg is None:
        cycle_median = None
        cycle_p90 = None
        cycle_tail_count = 0
        cycle_tail_pct = None
    else:
        d_sorted = sorted(durations)
        mid = len(d_sorted) // 2
        if len(d_sorted) % 2 == 0:
            cycle_median = round((d_sorted[mid - 1] + d_sorted[mid]) / 2.0, 1)
        else:
            cycle_median = round(d_sorted[mid], 1)
        p90_idx = max(0, int(math.ceil(0.9 * len(d_sorted))) - 1)
        cycle_p90 = round(d_sorted[p90_idx], 1)
        cycle_tail_count = int(sum(1 for d in d_sorted if d >= 8.0))
        cycle_tail_pct = round((float(cycle_tail_count) * 100.0) / float(len(d_sorted)), 1) if d_sorted else None
    return {
        "avg_hours": cycle_avg,
        "median_hours": cycle_median,
        "p90_hours": cycle_p90,
        "tail_count": cycle_tail_count,
        "tail_pct": cycle_tail_pct,
        "sample_count": cycle_total,
        "groups": cycle_groups,
    }


def _viz_domain_spider(trend_series: list[dict[str, Any]]) -> dict[str, Any]:
    """Domain health spider chart (current snapshot)."""
    spider_w = 300
    spider_h = 300
    spider_cx = 150.0
    spider_cy = 150.0
    spider_r = 108.0
    spider_inner_r = spider_r / 5.0  # first ring = 100% health
    spider_source = sorted(
        [{"domain": str(s["domain"]), "current": float(s["current"])} for s in trend_series],
        key=lambda x: str(x["domain"]),
    )
    spider_axes: list[dict[str, Any]] = []
    spider_pts: list[str] = []
    axis_count = len(spider_source)
    spider_outer_health = min([float(x["current"]) for x in spider_source] + [100.0])
    spider_scale_range = max(0.1, 100.0 - spider_outer_health)
    for i, s in enumerate(spider_source):
        ang = (-math.pi / 2.0) + (2.0 * math.pi * float(i) / float(axis_count)) if axis_count else 0.0
        axis_x = spider_cx + (spider_r * math.cos(ang))
        axis_y = spider_cy + (spider_r * math.sin(ang))
        health = float(s["current"])
        value_r = spider_inner_r + (max(0.0, min(spider_scale_range, (100.0 - health))) / spider_scale_range) * (spider_r - spider_inner_r)
        value_x = spider_cx + (value_r * math.cos(ang))
        value_y = spider_cy + (value_r * math.sin(ang))
        label_x = spider_cx + ((spider_r + 16.0) * math.cos(ang))
        label_y = spider_cy + ((spider_r + 16.0) * math.sin(ang))
        cos_v = math.cos(ang)
        if cos_v > 0.3:
            anchor = "start"
        elif cos_v < -0.3:
            anchor = "end"
        else:
            anchor = "middle"
        dname = str(s["domain"])
        focus = health < 85.0
        spider_pts.append(f"{value_x:.2f},{value_y:.2f}")
        spider_axes.append(
            {
                "domain": dname,
                "health": round(health, 1),
                "axis_x": round(axis_x, 2),
                "axis_y": round(axis_y, 2),
                "point_x": round(value_x, 2),
                "point_y": round(value_y, 2),
                "label_x": round(label_x, 2),
                "label_y": round(label_y, 2),
                "anchor": anchor,
                "focus": focus,
            }
        )
    spider_rings = []
    for pct_out in (20, 40, 60, 80, 100):
        # ring 1 (pct_out=20) = 100%, ring 5 (pct_out=100) = outer_health
        health_mark = 100.0 - ((float(pct_out - 20) / 80.0) * spider_scale_range)
        spider_rings.append(
            {
                "r": round((float(pct_out) / 100.0) * spider_r, 2),
                "health": round(health_mark, 1),
            }
        )
    spider_focus = [x for x in spider_axes if bool(x["focus"])]
    return {
        "width": spider_w,
        "height": spider_h,
        "cx": spider_cx,
        "cy": spider_cy,
        "r": spider_r,
        "rings": spider_rings,
        "axes": spider_axes,
        "polygon": " ".join(spider_pts),
        "focus": spider_focus,
        "outer_health": round(spider_outer_health, 1),
    }


def _viz_pressure_heat(
    trend_series: list[dict[str, Any]],
    health_matrix: dict[str, list[float]],
    hist_days: list[str],
) -> dict[str, Any]:
    """Domain-health pressure heat grid."""
    heat_rows: list[dict[str, Any]] = []
    for s in trend_series:
        d = str(s["domain"])
        vals = health_matrix.get(d, [])
        cells: list[dict[str, Any]] = []
        for v in vals:
            if v >= 95.0:
                level = "green"
            elif v >= 80.0:
                level = "amber"
            else:
                level = "red"
            cells.append({"value": round(v, 1), "level": level})
        current = round(float(vals[-1]), 1) if vals else 100.0
        if vals and hist_days:
            worst_val = min(float(v) for v in vals)
            worst_idx = next((i for i, v in enumerate(vals) if float(v) == worst_val), 0)
            worst_day = hist_days[worst_idx] if worst_idx < len(hist_days) else hist_days[-1]
        else:
            worst_val = 100.0
            worst_day = ""
        heat_rows.append(
            {
                "domain": d,
                "cells": cells,
                "current": current,
                "worst": round(worst_val, 1),
                "worst_day": worst_day,
            }
        )
    heat_focus = sorted([r for r in heat_rows if float(r["current"]) < 95.0], key=lambda x: float(x["current"]))[:3]
    return {"days": hist_days, "rows": heat_rows, "focus_rows": heat_focus}


def _admin_dashboard_visuals(
    conn: sqlite3.Connection,
    *,
    active_domains: list[str],
    domain_pressure_rows: list[dict[str, Any]],
    tasks_status: dict[str, int],
    workflows_status: dict[str, int],
    assessments_status: dict[str, int],
    returned_tasks: int,
    returned_workflows: int,
    returned_assessments: int,
    system_health: dict[str, Any],
    trend_days: int = 14,
) -> dict[str, Any]:
    """Build admin-only analytics visuals from existing state/audit tables."""
    coverage_rank = _viz_coverage_gaps(system_health)
    pipeline_flow, returns_pie = _viz_pipeline_flow(
        tasks_status,
        workflows_status,
        assessments_status,
        returned_tasks,
        returned_workflows,
        returned_assessments,
        domain_pressure_rows,
    )
    cycle_histogram = _viz_cycle_histogram(conn, trend_days)

    # --- Domain health trend + pressure heat grid (daily snapshots) ---
    domain_set = {str(d).strip().lower() for d in active_domains if str(d).strip()}
    domain_set.update({str(x.get("domain") or "").strip().lower() for x in domain_pressure_rows if str(x.get("domain") or "").strip()})
    hist_days = [d[0] for d in conn.execute(f"SELECT date('now','-{trend_days-1} day','+'||n||' day') FROM (WITH RECURSIVE cnt(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM cnt WHERE n<{trend_days-1}) SELECT n FROM cnt)").fetchall()]
    # Fallback if recursive CTE is unavailable for any reason.
    if not hist_days:
        hist_days = [x[0] for x in conn.execute("SELECT date('now')").fetchall()]

    health_matrix: dict[str, list[float]] = {d: [] for d in sorted(domain_set)}

    for day in hist_days:
        cutoff = f"{day}T23:59:59+00:00"
        per_domain: dict[str, dict[str, int]] = {
            d: {
                "submitted_tasks": 0,
                "submitted_workflows": 0,
                "submitted_assessments": 0,
                "returned_total": 0,
                "confirmed_tasks": 0,
                "confirmed_workflows": 0,
                "confirmed_assessments": 0,
            }
            for d in health_matrix.keys()
        }

        t_rows = conn.execute(
            """
            SELECT t.record_id, t.version, t.status, t.domain
            FROM tasks t
            JOIN (
              SELECT record_id, MAX(version) AS v
              FROM tasks
              WHERE updated_at <= ?
              GROUP BY record_id
            ) l ON l.record_id=t.record_id AND l.v=t.version
            """,
            (cutoff,),
        ).fetchall()
        for t in t_rows:
            d = str(t["domain"] or "").strip().lower()
            if not d:
                continue
            if d not in per_domain:
                per_domain[d] = {
                    "submitted_tasks": 0,
                    "submitted_workflows": 0,
                    "submitted_assessments": 0,
                    "returned_total": 0,
                    "confirmed_tasks": 0,
                    "confirmed_workflows": 0,
                    "confirmed_assessments": 0,
                }
            st = str(t["status"] or "")
            if st == "submitted":
                per_domain[d]["submitted_tasks"] += 1
            elif st == "returned":
                per_domain[d]["returned_total"] += 1
            elif st == "confirmed":
                per_domain[d]["confirmed_tasks"] += 1

        w_rows = conn.execute(
            """
            SELECT w.record_id, w.version, w.status, COALESCE(w.domains_json,'[]') AS domains_json
            FROM workflows w
            JOIN (
              SELECT record_id, MAX(version) AS v
              FROM workflows
              WHERE updated_at <= ?
              GROUP BY record_id
            ) l ON l.record_id=w.record_id AND l.v=w.version
            """,
            (cutoff,),
        ).fetchall()
        for w in w_rows:
            wdoms = _normalize_domains(w["domains_json"])
            st = str(w["status"] or "")
            for d in wdoms:
                if d not in per_domain:
                    per_domain[d] = {
                        "submitted_tasks": 0,
                        "submitted_workflows": 0,
                        "submitted_assessments": 0,
                        "returned_total": 0,
                        "confirmed_tasks": 0,
                        "confirmed_workflows": 0,
                        "confirmed_assessments": 0,
                    }
                if st == "submitted":
                    per_domain[d]["submitted_workflows"] += 1
                elif st == "returned":
                    per_domain[d]["returned_total"] += 1
                elif st == "confirmed":
                    per_domain[d]["confirmed_workflows"] += 1

        a_rows = conn.execute(
            """
            SELECT a.record_id, a.version, a.status, COALESCE(a.domains_json,'[]') AS domains_json
            FROM assessment_items a
            JOIN (
              SELECT record_id, MAX(version) AS v
              FROM assessment_items
              WHERE updated_at <= ?
              GROUP BY record_id
            ) l ON l.record_id=a.record_id AND l.v=a.version
            """,
            (cutoff,),
        ).fetchall()
        for a in a_rows:
            adoms = _normalize_domains(a["domains_json"])
            st = str(a["status"] or "")
            for d in adoms:
                if d not in per_domain:
                    per_domain[d] = {
                        "submitted_tasks": 0,
                        "submitted_workflows": 0,
                        "submitted_assessments": 0,
                        "returned_total": 0,
                        "confirmed_tasks": 0,
                        "confirmed_workflows": 0,
                        "confirmed_assessments": 0,
                    }
                if st == "submitted":
                    per_domain[d]["submitted_assessments"] += 1
                elif st == "returned":
                    per_domain[d]["returned_total"] += 1
                elif st == "confirmed":
                    per_domain[d]["confirmed_assessments"] += 1

        for d, m in per_domain.items():
            total_items = (
                m["submitted_tasks"]
                + m["submitted_workflows"]
                + m["submitted_assessments"]
                + m["returned_total"]
                + m["confirmed_tasks"]
                + m["confirmed_workflows"]
                + m["confirmed_assessments"]
            )
            confirmed_items = m["confirmed_tasks"] + m["confirmed_workflows"] + m["confirmed_assessments"]
            if total_items > 0:
                hp = round((float(confirmed_items) / float(total_items)) * 100.0, 1)
            else:
                hp = 100.0
            health_matrix.setdefault(d, []).append(hp)

    # Ensure every domain has equal length.
    for d in list(health_matrix.keys()):
        vals = health_matrix[d]
        if len(vals) < len(hist_days):
            vals.extend([100.0] * (len(hist_days) - len(vals)))

    # Build current per-domain ordering from the daily matrix.
    trend_series: list[dict[str, Any]] = []
    for d in sorted(health_matrix.keys()):
        vals = health_matrix[d]
        current = float(vals[-1]) if vals else 100.0
        trend_series.append(
            {
                "domain": d,
                "current": round(current, 1),
                "is_focus": bool(current < 95.0),
            }
        )
    # Keep risky domains first in legend/order.
    trend_series.sort(key=lambda x: (not bool(x["is_focus"]), float(x["current"]), str(x["domain"])))

    domain_spider = _viz_domain_spider(trend_series)
    pressure_heat = _viz_pressure_heat(trend_series, health_matrix, hist_days)

    return {
        "coverage_rank": coverage_rank,
        "pipeline_flow": pipeline_flow,
        "returns_pie": returns_pie,
        "cycle_histogram": cycle_histogram,
        "domain_spider": domain_spider,
        "pressure_heat": pressure_heat,
    }


# ---------------------------------------------------------------------------
# Admin dashboard panels
# ---------------------------------------------------------------------------

def _empty_pressure_entry() -> dict[str, int]:
    return {
        "submitted_tasks": 0,
        "submitted_workflows": 0,
        "submitted_assessments": 0,
        "returned_total": 0,
        "blocked_workflows": 0,
        "confirmed_tasks": 0,
        "confirmed_workflows": 0,
        "confirmed_assessments": 0,
    }


def _count_entity_status(conn: sqlite3.Connection, entity: str, status: str, role: str, dset: set[str]) -> int:
    """Count latest-version records of the given entity type matching the given status.

    Domain filtering is applied for non-admin roles. Tasks use a single `domain` column;
    workflows and assessment_items use a JSON `domains_json` array.
    """
    uses_json_domains = entity in ("workflows", "assessment_items")
    domain_col = "domains_json" if uses_json_domains else "domain"
    rows = conn.execute(
        f"SELECT record_id, MAX(version) AS latest_version FROM {entity} GROUP BY record_id"
    ).fetchall()
    c = 0
    for r in rows:
        latest = conn.execute(
            f"SELECT status, {domain_col} FROM {entity} WHERE record_id=? AND version=?",
            (r["record_id"], int(r["latest_version"])),
        ).fetchone()
        if not latest or str(latest["status"]) != status:
            continue
        if role == "admin":
            c += 1
            continue
        if uses_json_domains:
            if dset.intersection(_normalize_domains(latest["domains_json"])):
                c += 1
        else:
            if str(latest["domain"] or "").strip().lower() in dset:
                c += 1
    return c


def _compute_admin_panels(conn: sqlite3.Connection, doms: list[str], system_health: dict[str, Any]) -> dict[str, Any]:
    """Compute all admin dashboard panel data from the active DB connection."""
    dset: set[str] = {d.strip().lower() for d in doms if d}

    domain_pressure: dict[str, dict[str, Any]] = {d: _empty_pressure_entry() for d in doms}

    # --- Workflows ---
    workflow_rows = conn.execute(
        "SELECT record_id, MAX(version) AS latest_version FROM workflows GROUP BY record_id"
    ).fetchall()
    awaiting_task_confirmation = 0
    invalid_workflows = 0
    for r in workflow_rows:
        rid = str(r["record_id"])
        latest_v = int(r["latest_version"])
        w = conn.execute(
            "SELECT status, domains_json FROM workflows WHERE record_id=? AND version=?",
            (rid, latest_v),
        ).fetchone()
        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (rid, latest_v),
        ).fetchall()
        pairs = [(x["task_record_id"], int(x["task_version"])) for x in refs]
        readiness = workflow_readiness(conn, pairs)
        if readiness == "awaiting_task_confirmation":
            awaiting_task_confirmation += 1
        elif readiness == "invalid":
            invalid_workflows += 1

        wdoms = _normalize_domains(w["domains_json"] if w else None)
        if not wdoms:
            wdoms = [str(x).strip().lower() for x in (_workflow_domains(conn, pairs) or []) if str(x).strip()]
        wstatus = str(w["status"] if w else "")
        for d in wdoms:
            if d not in domain_pressure:
                domain_pressure[d] = _empty_pressure_entry()
            if wstatus == "submitted":
                domain_pressure[d]["submitted_workflows"] += 1
                if readiness == "awaiting_task_confirmation":
                    domain_pressure[d]["blocked_workflows"] += 1
            elif wstatus == "returned":
                domain_pressure[d]["returned_total"] += 1
            elif wstatus == "confirmed":
                domain_pressure[d]["confirmed_workflows"] += 1

    # --- Tasks ---
    task_rows = conn.execute(
        "SELECT record_id, MAX(version) AS latest_version FROM tasks GROUP BY record_id"
    ).fetchall()
    tasks_missing_domain = 0
    for r in task_rows:
        t = conn.execute(
            "SELECT status, domain FROM tasks WHERE record_id=? AND version=?",
            (r["record_id"], int(r["latest_version"])),
        ).fetchone()
        if not t or not str(t["domain"] or "").strip():
            tasks_missing_domain += 1
        d = str((t["domain"] if t else "") or "").strip().lower()
        if d:
            if d not in domain_pressure:
                domain_pressure[d] = _empty_pressure_entry()
            if str(t["status"]) == "submitted":
                domain_pressure[d]["submitted_tasks"] += 1
            elif str(t["status"]) == "returned":
                domain_pressure[d]["returned_total"] += 1
            elif str(t["status"]) == "confirmed":
                domain_pressure[d]["confirmed_tasks"] += 1

    # --- Assessments ---
    assessment_rows = conn.execute(
        "SELECT record_id, MAX(version) AS latest_version FROM assessment_items GROUP BY record_id"
    ).fetchall()
    assessments_missing_domain = 0
    for r in assessment_rows:
        a = conn.execute(
            "SELECT status, domains_json FROM assessment_items WHERE record_id=? AND version=?",
            (r["record_id"], int(r["latest_version"])),
        ).fetchone()
        dom_list = _normalize_domains(a["domains_json"] if a else None)
        if not dom_list:
            assessments_missing_domain += 1
        for d in dom_list:
            if d not in domain_pressure:
                domain_pressure[d] = _empty_pressure_entry()
            if str(a["status"]) == "submitted":
                domain_pressure[d]["submitted_assessments"] += 1
            elif str(a["status"]) == "returned":
                domain_pressure[d]["returned_total"] += 1
            elif str(a["status"]) == "confirmed":
                domain_pressure[d]["confirmed_assessments"] += 1

    # --- Domain pressure rows ---
    domain_pressure_rows: list[dict[str, Any]] = []
    for d, m in domain_pressure.items():
        total_items = (
            m["submitted_tasks"] + m["submitted_workflows"] + m["submitted_assessments"] +
            m["returned_total"] + m["confirmed_tasks"] + m["confirmed_workflows"] + m["confirmed_assessments"]
        )
        confirmed_items = m["confirmed_tasks"] + m["confirmed_workflows"] + m["confirmed_assessments"]
        health_pct = round((confirmed_items / total_items) * 100, 1) if total_items > 0 else 100.0
        if health_pct >= 95:
            level = "green"
        elif health_pct >= 85:
            level = "amber"
        else:
            level = "red"
        domain_pressure_rows.append({"domain": d, "health_pct": health_pct, "level": level, **m, "href": f"/tasks?status=submitted&domain={d}"})
    domain_pressure_rows.sort(key=lambda x: (x["health_pct"], x["domain"]))

    # --- Status counts ---
    tasks_status = {s: _count_entity_status(conn, "tasks", s, "admin", dset) for s in ("draft", "submitted", "returned", "confirmed")}
    workflows_status = {s: _count_entity_status(conn, "workflows", s, "admin", dset) for s in ("draft", "submitted", "returned", "confirmed")}
    assessments_status = {s: _count_entity_status(conn, "assessment_items", s, "admin", dset) for s in ("draft", "submitted", "returned", "confirmed")}

    returned_tasks = tasks_status["returned"]
    returned_workflows = workflows_status["returned"]
    returned_assessments = assessments_status["returned"]

    admin_panels: dict[str, Any] = {
        "status_breakdown": [
            {"title": "Tasks", "href": "/tasks", **tasks_status},
            {"title": "Workflows", "href": "/workflows", **workflows_status},
            {"title": "Assessments", "href": "/assessments", **assessments_status},
        ],
        "health": [
            {"title": "Tasks submitted", "value": tasks_status["submitted"], "href": "/tasks?status=submitted"},
            {"title": "Workflows submitted", "value": workflows_status["submitted"], "href": "/workflows?status=submitted"},
            {"title": "Assessments submitted", "value": assessments_status["submitted"], "href": "/assessments?status=submitted"},
        ],
        "blockers": [
            {"title": "Workflows blocked by tasks", "value": awaiting_task_confirmation, "href": "/workflows?status=submitted"},
            {"title": "Returned tasks", "value": returned_tasks, "href": "/tasks?status=returned"},
            {"title": "Returned workflows", "value": returned_workflows, "href": "/workflows?status=returned"},
            {"title": "Returned assessments", "value": returned_assessments, "href": "/assessments?status=returned"},
        ],
        "blocker_total": int(awaiting_task_confirmation + returned_tasks + returned_workflows + returned_assessments),
        "integrity": [
            {"title": "Invalid workflows", "value": invalid_workflows, "href": "/workflows"},
            {"title": "Tasks missing domain", "value": tasks_missing_domain, "href": "/tasks"},
            {"title": "Assessments missing domain", "value": assessments_missing_domain, "href": "/assessments"},
        ],
        "domain_pressure": domain_pressure_rows,
        "alert_blocked_workflows": awaiting_task_confirmation,
        "alert_returned_tasks": returned_tasks,
        "alert_returned_workflows": returned_workflows,
        "alert_returned_assessments": returned_assessments,
        "alert_submitted_workflows": workflows_status["submitted"],
        "alert_draft_assessments": assessments_status["draft"],
    }
    admin_panels["viz"] = _admin_dashboard_visuals(
        conn,
        active_domains=doms,
        domain_pressure_rows=domain_pressure_rows,
        tasks_status=tasks_status,
        workflows_status=workflows_status,
        assessments_status=assessments_status,
        returned_tasks=returned_tasks,
        returned_workflows=returned_workflows,
        returned_assessments=returned_assessments,
        system_health=system_health,
    )
    return admin_panels
