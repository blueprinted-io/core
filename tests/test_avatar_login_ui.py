from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import lcs_mvp.app.main as app_main


def _first_active_username() -> str:
    with sqlite3.connect(app_main.DB_DEBIAN_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT username FROM users WHERE disabled_at IS NULL ORDER BY id ASC LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row["username"])


def _set_avatar_path(username: str, avatar_path: str) -> None:
    with sqlite3.connect(app_main.DB_DEBIAN_PATH) as conn:
        conn.execute(
            "UPDATE users SET avatar_path=? WHERE username=?",
            (avatar_path, username),
        )
        conn.commit()


def test_public_avatar_route_is_accessible_without_auth(client: TestClient) -> None:
    username = _first_active_username()
    r = client.get(f"/avatar/{username}", headers={"accept": "application/json"}, follow_redirects=False)
    # Public route should not redirect/deny unauthenticated access.
    assert r.status_code in (200, 404)


def test_public_avatar_serves_image_with_safe_path(client: TestClient) -> None:
    username = _first_active_username()
    avatar_file = Path(app_main.UPLOADS_DIR) / "avatars" / f"{username}.png"
    avatar_file.parent.mkdir(parents=True, exist_ok=True)
    avatar_file.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    _set_avatar_path(username, str(avatar_file))

    r = client.get(f"/avatar/{username}", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/png")
    assert "no-cache" in (r.headers.get("cache-control") or "")


def test_public_avatar_rejects_path_outside_uploads(client: TestClient) -> None:
    username = _first_active_username()
    outside_file = Path(app_main.DATA_DIR) / "outside.png"
    outside_file.write_bytes(b"not-an-avatar")
    _set_avatar_path(username, str(outside_file))

    r = client.get(f"/avatar/{username}", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 400
    assert r.json().get("detail") == "Invalid avatar path"


def test_profile_avatar_requires_auth(client: TestClient) -> None:
    r = client.get("/profile/avatar", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_login_uses_avatar_cards_with_fallback_markup(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    body = r.text
    assert 'class="demo-user-card js-quick-login"' in body
    assert 'class="demo-user-avatar"' in body
    assert "/avatar/" in body
    assert "demo-user-avatar-fallback" in body
    assert "onerror=\"this.style.display='none';this.nextElementSibling.style.display='flex'\"" in body
