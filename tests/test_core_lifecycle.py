from __future__ import annotations

import re
import sqlite3

from fastapi.testclient import TestClient

import lcs_mvp.app.main as app_main


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _logout(client: TestClient) -> None:
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303


def _create_task(client: TestClient, domain: str) -> tuple[str, int]:
    r = client.post(
        "/tasks/new",
        data={
            "title": f"Task for {domain}",
            "outcome": "Outcome",
            "procedure_name": "procedure",
            "domain": domain,
            "facts": "Fact A",
            "concepts": "Concept A",
            "dependencies": "Dependency A",
            "step_text": ["Do thing"],
            "step_completion": ["Thing is done"],
            "step_actions": ["echo done"],
            "step_notes": [""],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    m = re.search(r"/tasks/([0-9a-f-]+)/(\d+)/edit", loc)
    assert m, f"unexpected create task redirect: {loc}"
    return m.group(1), int(m.group(2))


def _create_workflow(client: TestClient, task_record_id: str, task_version: int) -> tuple[str, int]:
    r = client.post(
        "/workflows/new",
        data={
            "title": "Workflow A",
            "objective": "Objective A",
            "task_refs": f"{task_record_id}@{task_version}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    m = re.search(r"/workflows/([0-9a-f-]+)/(\d+)$", loc)
    assert m, f"unexpected create workflow redirect: {loc}"
    return m.group(1), int(m.group(2))


def test_task_submit_rejects_unauthorized_domain(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")
    rid, ver = _create_task(client, "aws")

    r = client.post(f"/tasks/{rid}/{ver}/submit")
    assert r.status_code == 403
    assert "not authorized for domain 'aws'" in r.json()["detail"]


def test_task_submit_then_confirm_happy_path(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")
    rid, ver = _create_task(client, "debian")

    r_submit = client.post(f"/tasks/{rid}/{ver}/submit", follow_redirects=False)
    assert r_submit.status_code == 303

    _logout(client)
    _login(client, "jhendrix", "password1")

    r_confirm = client.post(f"/tasks/{rid}/{ver}/confirm", follow_redirects=False)
    assert r_confirm.status_code == 303

    conn = sqlite3.connect(app_main.DB_DEBIAN_PATH)
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?",
            (rid, ver),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert str(row[0]) == "confirmed"


def test_workflow_confirm_blocked_until_referenced_task_confirmed(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")
    task_rid, task_ver = _create_task(client, "debian")

    r_task_submit = client.post(f"/tasks/{task_rid}/{task_ver}/submit", follow_redirects=False)
    assert r_task_submit.status_code == 303

    wf_rid, wf_ver = _create_workflow(client, task_rid, task_ver)
    r_wf_submit = client.post(f"/workflows/{wf_rid}/{wf_ver}/submit", follow_redirects=False)
    assert r_wf_submit.status_code == 303

    _logout(client)
    _login(client, "jhendrix", "password1")

    r_wf_confirm_blocked = client.post(f"/workflows/{wf_rid}/{wf_ver}/confirm")
    assert r_wf_confirm_blocked.status_code == 409
    assert "Task versions must be confirmed" in r_wf_confirm_blocked.json()["detail"]

    r_task_confirm = client.post(f"/tasks/{task_rid}/{task_ver}/confirm", follow_redirects=False)
    assert r_task_confirm.status_code == 303

    r_wf_confirm = client.post(f"/workflows/{wf_rid}/{wf_ver}/confirm", follow_redirects=False)
    assert r_wf_confirm.status_code == 303

    conn = sqlite3.connect(app_main.DB_DEBIAN_PATH)
    try:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?",
            (wf_rid, wf_ver),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert str(row[0]) == "confirmed"
