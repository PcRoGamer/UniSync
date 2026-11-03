"""UniSync Standalone Web Dashboard & Console.

FastAPI HTML response endpoint providing:
- High-density lecture video gallery with authenticated Echo360 thumbnail posters
- Interactive in-browser lecture transcript & subtitles modal
- Dynamic course cards with Canvas LMS image banners & LTI launcher
- Searchable & filterable inventory table of all downloads
- Real-time push WebSocket sync progress bar and terminal activity log
- Extracted Academic Deadlines & Todos management
- EdStem & University Email monitor settings & polling triggers
- 1-click userscript installation

The dashboard HTML lives in assets/dashboard.html as a standalone file.
This avoids Python triple-quoted string escaping issues where backslash
sequences (\\n, \\r, \\t) in embedded JS get silently eaten by Python.
"""

from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, FileResponse

router = APIRouter(tags=["dashboard"])

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_DASHBOARD_PATH = _ASSETS_DIR / "dashboard.html"
_USERSCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "userscript"
    / "echo360-connector.user.js"
)


@router.get("/userscript/echo360-connector.user.js", deprecated=True)
async def get_userscript():
    """Serves the legacy browser userscript for backward compatibility (DEPRECATED).
    
    Replaced by Camoufox stealth browser (unisync.services.browser.camoufox_service).
    """
    if _USERSCRIPT_PATH.exists():
        return FileResponse(
            _USERSCRIPT_PATH,
            media_type="application/javascript",
            filename="echo360-connector.user.js",
            headers={"Deprecation": "true", "Warning": '299 - "Legacy Tampermonkey userscript is deprecated. Use Camoufox stealth browser."'},
        )
    return HTMLResponse("Userscript not found", status_code=404)


@router.get("/userscript/bridge-runtime.js", deprecated=True)
async def get_bridge_runtime():
    """Serves the legacy runtime bridge for backward compatibility (DEPRECATED)."""
    return FileResponse(
        _ASSETS_DIR / "echo360_userscript" / "bridge-runtime.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store",
            "Deprecation": "true",
            "Warning": '299 - "Legacy Tampermonkey runtime is deprecated. Use Camoufox stealth browser."',
        },
    )


@router.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    """Renders the single-page UniSync Dashboard from the static HTML asset."""
    if _DASHBOARD_PATH.exists():
        return FileResponse(
            _DASHBOARD_PATH,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return HTMLResponse(
        "<h1>Dashboard HTML not found</h1>"
        f"<p>Expected at: {_DASHBOARD_PATH}</p>",
        status_code=500,
    )
