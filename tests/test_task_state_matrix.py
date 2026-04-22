from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient


def _revise_task(client: TestClient, rid: str, version: int, note: str = "revise") -> int:
    r = client.post(
        f"/tasks/{rid}/{version}/save",
        data={
            "title": "Task revised",
            "outcome": "Outcome revised",
            "procedure_name": "procedure",
            "domain": "debian",
            "facts": "Fact A",
            "concepts": "Concept A",
            "dependencies": "Dependency A",
            "step_text": ["Do thing"],
            "step_completion": ["Thing is done and verified"],
            "step_actions": ["echo done"],
            "step_notes": [""],
            "change_note": note,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    m = re.search(rf"/tasks/{rid}/(\d+)$", loc)
    assert m, f"unexpected revise redirect: {loc}"
    return int(m.group(1))


@pytest.mark.parametrize(
    "user,pw,domain,expected_status",
    [
        ("jjoplin", "password2", "debian", 303),  # Y: contributor with entitled domain
        ("jjoplin", "password2", "aws", 403),     # N: contributor lacking domain entitlement
        ("jhendrix", "password1", "debian", 303), # Y: contributor with entitled domain can also submit
        ("fmercury", "password3", "debian", 403), # N: viewer cannot submit
    ],
)
def test_stage_draft_submit_yes_no(
    client: TestClient, login, logout, create_task, user: str, pw: str, domain: str, expected_status: int
) -> None:
    login("jjoplin", "password2")
    rid, ver = create_task(domain)
    logout()

    login(user, pw)
    r = client.post(f"/tasks/{rid}/{ver}/submit", follow_redirects=False)
    assert r.status_code == expected_status


def test_stage_submitted_return_yes_no(client: TestClient, login, logout, create_task) -> None:
    login("jjoplin", "password2")
    rid, ver = create_task("debian")
    assert client.post(f"/tasks/{rid}/{ver}/submit", follow_redirects=False).status_code == 303
    logout()

    # Y: reviewer can return with note.
    login("jhendrix", "password1")
    r_yes = client.post(
        f"/tasks/{rid}/{ver}/return",
        data={"note": "Needs clearer completion criteria."},
        follow_redirects=False,
    )
    assert r_yes.status_code == 303
    logout()

    # N: returned version cannot be returned again from wrong status.
    login("jhendrix", "password1")
    r_wrong_status = client.post(
        f"/tasks/{rid}/{ver}/return",
        data={"note": "second return"},
    )
    assert r_wrong_status.status_code == 409
    logout()

    # N: contributor cannot return content they created (self-review prohibition).
    # jjoplin created the task, so jjoplin cannot return it even after it is
    # revised and re-submitted. Create a fresh task, submit it, and verify.
    login("jjoplin", "password2")
    rid2, ver2 = create_task("debian")
    assert client.post(f"/tasks/{rid2}/{ver2}/submit", follow_redirects=False).status_code == 303
    r_self_review = client.post(
        f"/tasks/{rid2}/{ver2}/return",
        data={"note": "self-review attempt"},
    )
    assert r_self_review.status_code == 403


def test_stage_submitted_confirm_yes_no(client: TestClient, login, logout, create_task) -> None:
    # Y: reviewer can confirm submitted debian task.
    login("jjoplin", "password2")
    rid_ok, ver_ok = create_task("debian")
    assert client.post(f"/tasks/{rid_ok}/{ver_ok}/submit", follow_redirects=False).status_code == 303
    logout()

    login("jhendrix", "password1")
    r_yes = client.post(f"/tasks/{rid_ok}/{ver_ok}/confirm", follow_redirects=False)
    assert r_yes.status_code == 303
    logout()

    # N: author cannot confirm.
    login("jjoplin", "password2")
    rid_role, ver_role = create_task("debian")
    assert client.post(f"/tasks/{rid_role}/{ver_role}/submit", follow_redirects=False).status_code == 303
    r_wrong_role = client.post(f"/tasks/{rid_role}/{ver_role}/confirm")
    assert r_wrong_role.status_code == 403
    logout()

    # N: reviewer cannot confirm submitted task outside their domain entitlement.
    login("kcobain", "admin")
    rid_dom, ver_dom = create_task("aws")
    assert client.post(f"/tasks/{rid_dom}/{ver_dom}/force-submit", follow_redirects=False).status_code == 303
    logout()

    login("jhendrix", "password1")
    r_wrong_domain = client.post(f"/tasks/{rid_dom}/{ver_dom}/confirm")
    assert r_wrong_domain.status_code == 403


def test_stage_returned_revise_and_resubmit_yes_no(client: TestClient, login, logout, create_task) -> None:
    login("jjoplin", "password2")
    rid, ver1 = create_task("debian")
    assert client.post(f"/tasks/{rid}/{ver1}/submit", follow_redirects=False).status_code == 303
    logout()

    login("jhendrix", "password1")
    assert client.post(
        f"/tasks/{rid}/{ver1}/return",
        data={"note": "Please revise."},
        follow_redirects=False,
    ).status_code == 303
    logout()

    login("jjoplin", "password2")
    # N: old returned version cannot be submitted directly.
    r_old_submit = client.post(f"/tasks/{rid}/{ver1}/submit")
    assert r_old_submit.status_code == 409

    # Y: revise returned version -> new draft version, then submit.
    ver2 = _revise_task(client, rid, ver1, note="address reviewer feedback")
    assert ver2 == ver1 + 1
    r_submit_v2 = client.post(f"/tasks/{rid}/{ver2}/submit", follow_redirects=False)
    assert r_submit_v2.status_code == 303


def test_stage_confirmed_followups_yes_no(client: TestClient, login, logout, create_task) -> None:
    login("jjoplin", "password2")
    rid, ver1 = create_task("debian")
    assert client.post(f"/tasks/{rid}/{ver1}/submit", follow_redirects=False).status_code == 303
    logout()

    login("jhendrix", "password1")
    assert client.post(f"/tasks/{rid}/{ver1}/confirm", follow_redirects=False).status_code == 303
    logout()

    login("jjoplin", "password2")
    # N: confirmed version cannot be submitted again.
    r_submit_old = client.post(f"/tasks/{rid}/{ver1}/submit")
    assert r_submit_old.status_code == 409

    # Y: confirmed version can be revised into a new draft.
    ver2 = _revise_task(client, rid, ver1, note="post-confirm update")
    assert ver2 == ver1 + 1
    logout()

    login("jhendrix", "password1")
    # N: reviewer cannot confirm the old already-confirmed version again.
    r_confirm_old = client.post(f"/tasks/{rid}/{ver1}/confirm")
    assert r_confirm_old.status_code == 409
