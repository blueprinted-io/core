from __future__ import annotations

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


def test_profile_domain_save_blocked_for_non_admin(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")

    r = client.post("/profile/domains/save", data={"domain": "aws"})
    assert r.status_code == 403
    assert "admin-managed" in r.json()["detail"]


def test_admin_can_assign_domains_via_admin_endpoint(client: TestClient) -> None:
    _login(client, "kcobain", "admin")

    r = client.post(
        "/admin/users/domains/save",
        data={"username": "jjoplin", "domain": ["debian", "aws"]},
        follow_redirects=False,
    )
    assert r.status_code == 303

    conn = sqlite3.connect(app_main.DB_DEBIAN_PATH)
    try:
        rows = conn.execute(
            """
            SELECT d.domain
            FROM user_domains d
            JOIN users u ON u.id = d.user_id
            WHERE u.username = 'jjoplin'
            ORDER BY d.domain ASC
            """
        ).fetchall()
    finally:
        conn.close()

    assert [str(r[0]) for r in rows] == ["aws", "debian"]


def test_profile_shows_admin_managed_domains_message(client: TestClient) -> None:
    _login(client, "jjoplin", "password2")

    r = client.get("/profile")
    assert r.status_code == 200
    assert "Domain entitlements are admin-managed" in r.text
    assert 'action="/profile/domains/save"' not in r.text
