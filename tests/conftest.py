from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lcs_mvp.app.main as app_main


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data"
    uploads_dir = data_dir / "uploads"
    exports_dir = data_dir / "exports"
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    db_default = data_dir / "lcs_blueprinted_org.db"
    db_blank = data_dir / "lcs_blank.db"

    monkeypatch.setattr(app_main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_main, "UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setattr(app_main, "EXPORTS_DIR", str(exports_dir))
    monkeypatch.setattr(app_main, "DB_DEBIAN_PATH", str(db_default))
    monkeypatch.setattr(app_main, "DB_BLANK_PATH", str(db_blank))
    monkeypatch.setattr(app_main, "DB_OLD_DEBIAN_PATH", str(data_dir / "lcs_debian.db"))
    monkeypatch.setattr(app_main, "DB_DEMO_LEGACY_PATH", str(data_dir / "lcs_demo.db"))

    app_main.DB_PATH_CTX.set(str(db_default))
    app_main.DB_KEY_CTX.set(app_main.DB_KEY_DEBIAN)
    app_main.init_db()

    with TestClient(app_main.app) as c:
        yield c
