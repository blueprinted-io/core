from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    LMSTUDIO_BASE_URL, LMSTUDIO_MODEL, STATIC_ASSET_VERSION, templates,
)
from .database import init_db
from .auth import can, AuthMiddleware
from .routes import auth as auth_routes, admin as admin_routes
from .routes import home as home_routes
from .routes import tasks as tasks_routes
from .routes import workflows as workflows_routes
from .routes import assessments as assessments_routes
from .routes import imports as imports_routes
from .routes import exports as exports_routes

# ---------------------------------------------------------------------------
# Re-exports for test and external compatibility
# (tests access these as app_main.X after importing lcs_mvp.app.main as app_main)
# ---------------------------------------------------------------------------
from .config import (  # noqa: F401
    DATA_DIR, UPLOADS_DIR, EXPORTS_DIR,
    DB_DEBIAN_PATH, DB_BLANK_PATH, DB_OLD_DEBIAN_PATH, DB_DEMO_LEGACY_PATH,
    DB_PATH_CTX, DB_KEY_CTX, DB_KEY_DEBIAN,
)
from .database import init_db as _init_db_alias  # noqa: F401 (init_db already imported above)

@asynccontextmanager
async def _lifespan(application: FastAPI):
    init_db()
    yield


app = FastAPI(title="Learning Content System MVP", lifespan=_lifespan)


def _import_error_response(request: Request, detail: str, status_code: int):
    """Render the appropriate import form with an error message."""
    path = str(request.url.path)
    if path.startswith("/import/json"):
        template = "import_json.html"
        ctx: dict[str, Any] = {"error": detail}
    else:
        template = "import_pdf.html"
        ctx = {"error": detail, "lmstudio_base_url": LMSTUDIO_BASE_URL, "lmstudio_model": LMSTUDIO_MODEL}
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Prefer HTML error details for browser flows."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept and (
        str(request.url.path).startswith("/import/pdf")
        or str(request.url.path).startswith("/import/json")
    ):
        return _import_error_response(request, str(exc.detail), exc.status_code)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all so browser users get some error text instead of a blank 500 page."""
    import traceback

    traceback.print_exc()
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept and (
        str(request.url.path).startswith("/import/pdf")
        or str(request.url.path).startswith("/import/json")
    ):
        return _import_error_response(request, "An unexpected error occurred.", 500)
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})


static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.add_middleware(AuthMiddleware)

templates.env.globals["can"] = can
templates.env.globals["asset_v"] = STATIC_ASSET_VERSION

for _r in (
    auth_routes, admin_routes, home_routes, tasks_routes,
    workflows_routes, assessments_routes, imports_routes, exports_routes,
):
    app.include_router(_r.router)


