from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient


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


def _create_task(client: TestClient, domain: str, title: str = "Task") -> tuple[str, int]:
    r = client.post(
        "/tasks/new",
        data={
            "title": title,
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
    assert m, f"unexpected task create redirect: {loc}"
    return m.group(1), int(m.group(2))


def _revise_task(client: TestClient, rid: str, version: int, domain: str = "debian") -> int:
    r = client.post(
        f"/tasks/{rid}/{version}/save",
        data={
            "title": "Task revised",
            "outcome": "Outcome revised",
            "procedure_name": "procedure",
            "domain": domain,
            "facts": "Fact A",
            "concepts": "Concept A",
            "dependencies": "Dependency A",
            "step_text": ["Do thing"],
            "step_completion": ["Thing is done and verified"],
            "step_actions": ["echo done"],
            "step_notes": [""],
            "change_note": "revision",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    m = re.search(rf"/tasks/{rid}/(\d+)$", loc)
    assert m, f"unexpected task revise redirect: {loc}"
    return int(m.group(1))


def _create_workflow(client: TestClient, refs_text: str, title: str = "Workflow A") -> tuple[str, int]:
    r = client.post(
        "/workflows/new",
        data={
            "title": title,
            "objective": "Objective A",
            "task_refs": refs_text,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    m = re.search(r"/workflows/([0-9a-f-]+)/(\d+)$", loc)
    assert m, f"unexpected workflow create redirect: {loc}"
    return m.group(1), int(m.group(2))


@pytest.mark.parametrize(
    "submit_actor,submit_pw,task_domain,expected_status",
    [
        ("jjoplin", "password2", "debian", 303),  # Y: contributor with entitled domain
        ("jjoplin", "password2", "aws", 403),     # N: contributor lacking entitlement for workflow domain
        ("jhendrix", "password1", "debian", 303), # Y: contributor with entitled domain can also submit
        ("fmercury", "password3", "debian", 403), # N: viewer cannot submit workflows
    ],
)
def test_workflow_stage_draft_submit_yes_no(
    client: TestClient,
    submit_actor: str,
    submit_pw: str,
    task_domain: str,
    expected_status: int,
) -> None:
    _login(client, "jjoplin", "password2")
    task_rid, task_ver = _create_task(client, task_domain, title=f"Task {task_domain}")
    wf_rid, wf_ver = _create_workflow(client, f"{task_rid}@{task_ver}", title=f"WF {task_domain}")
    _logout(client)

    _login(client, submit_actor, submit_pw)
    r_submit = client.post(f"/workflows/{wf_rid}/{wf_ver}/submit", follow_redirects=False)
    assert r_submit.status_code == expected_status


def test_workflow_confirm_blocked_when_ref_task_not_confirmed(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")
    task_rid, task_ver = _create_task(client, "debian", title="Draft ref task")
    wf_rid, wf_ver = _create_workflow(client, f"{task_rid}@{task_ver}", title="WF with draft ref")
    assert client.post(f"/workflows/{wf_rid}/{wf_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    r_confirm = client.post(f"/workflows/{wf_rid}/{wf_ver}/confirm")
    assert r_confirm.status_code == 409
    assert "Task versions must be confirmed" in r_confirm.json()["detail"]


def test_workflow_confirm_yes_no_role_and_domain(client: TestClient) -> None:
    # Y: reviewer confirms submitted workflow over confirmed debian task.
    _login(client, "jjoplin", "password2")
    task_rid, task_ver = _create_task(client, "debian", title="Debian confirmed task")
    assert client.post(f"/tasks/{task_rid}/{task_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/tasks/{task_rid}/{task_ver}/confirm", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jjoplin", "password2")
    wf_rid, wf_ver = _create_workflow(client, f"{task_rid}@{task_ver}", title="WF confirm yes")
    assert client.post(f"/workflows/{wf_rid}/{wf_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/workflows/{wf_rid}/{wf_ver}/confirm", follow_redirects=False).status_code == 303
    _logout(client)

    # N: wrong role (author) cannot confirm submitted workflow.
    _login(client, "jjoplin", "password2")
    task2_rid, task2_ver = _create_task(client, "debian", title="Debian task role check")
    assert client.post(f"/tasks/{task2_rid}/{task2_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/tasks/{task2_rid}/{task2_ver}/confirm", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jjoplin", "password2")
    wf2_rid, wf2_ver = _create_workflow(client, f"{task2_rid}@{task2_ver}", title="WF role no")
    assert client.post(f"/workflows/{wf2_rid}/{wf2_ver}/submit", follow_redirects=False).status_code == 303
    r_wrong_role = client.post(f"/workflows/{wf2_rid}/{wf2_ver}/confirm")
    assert r_wrong_role.status_code == 403
    _logout(client)

    # N: reviewer cannot confirm workflow in unauthorized domain.
    _login(client, "kcobain", "admin")
    aws_rid, aws_ver = _create_task(client, "aws", title="AWS confirmed task")
    assert client.post(f"/tasks/{aws_rid}/{aws_ver}/force-submit", follow_redirects=False).status_code == 303
    assert client.post(f"/tasks/{aws_rid}/{aws_ver}/force-confirm", follow_redirects=False).status_code == 303
    wf3_rid, wf3_ver = _create_workflow(client, f"{aws_rid}@{aws_ver}", title="WF aws domain")
    assert client.post(f"/workflows/{wf3_rid}/{wf3_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    r_wrong_domain = client.post(f"/workflows/{wf3_rid}/{wf3_ver}/confirm")
    assert r_wrong_domain.status_code == 403


def test_workflow_confirm_allows_deprecated_task_refs(client: TestClient) -> None:
    # Build task v1 confirmed, then v2 confirmed -> v1 becomes deprecated.
    _login(client, "jjoplin", "password2")
    task_rid, v1 = _create_task(client, "debian", title="Task for deprecated-ref test")
    assert client.post(f"/tasks/{task_rid}/{v1}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/tasks/{task_rid}/{v1}/confirm", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jjoplin", "password2")
    v2 = _revise_task(client, task_rid, v1, domain="debian")
    assert client.post(f"/tasks/{task_rid}/{v2}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/tasks/{task_rid}/{v2}/confirm", follow_redirects=False).status_code == 303
    _logout(client)

    # Author creates workflow referencing deprecated v1; submit+confirm should still pass.
    _login(client, "jjoplin", "password2")
    wf_rid, wf_ver = _create_workflow(client, f"{task_rid}@{v1}", title="WF deprecated ref")
    assert client.post(f"/workflows/{wf_rid}/{wf_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    r_confirm = client.post(f"/workflows/{wf_rid}/{wf_ver}/confirm", follow_redirects=False)
    assert r_confirm.status_code == 303


def test_workflow_create_rejects_retired_or_missing_refs(client: TestClient) -> None:
    # Retired ref -> invalid at create.
    _login(client, "jjoplin", "password2")
    task_rid, task_ver = _create_task(client, "debian", title="Task for retire test")
    assert client.post(f"/tasks/{task_rid}/{task_ver}/submit", follow_redirects=False).status_code == 303
    _logout(client)

    _login(client, "jhendrix", "password1")
    assert client.post(f"/tasks/{task_rid}/{task_ver}/confirm", follow_redirects=False).status_code == 303
    assert client.post(
        f"/tasks/{task_rid}/{task_ver}/retire",
        data={"note": "Retired for test"},
        follow_redirects=False,
    ).status_code == 303
    _logout(client)

    _login(client, "jjoplin", "password2")
    r_retired = client.post(
        "/workflows/new",
        data={"title": "WF retired ref", "objective": "Obj", "task_refs": f"{task_rid}@{task_ver}"},
    )
    assert r_retired.status_code == 409
    assert "missing or retired" in r_retired.json()["detail"]

    # Missing ref -> invalid at create.
    fake = str(uuid.uuid4())
    r_missing = client.post(
        "/workflows/new",
        data={"title": "WF missing ref", "objective": "Obj", "task_refs": f"{fake}@1"},
    )
    assert r_missing.status_code == 409
    assert "missing or retired" in r_missing.json()["detail"]
