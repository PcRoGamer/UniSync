"""FastAPI router for Camoufox stealth browser automation and authentication."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from unisync.services.browser import get_camoufox_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])

# In-memory login job state — one login at a time
# ponytail: single global dict is fine; there's only one user and one Camoufox window
_login_job: dict[str, Any] = {"running": False, "result": None, "error": None}
_login_task: asyncio.Task | None = None


class LoginRequest(BaseModel):
    target_url: str = "https://canvas.lms.unimelb.edu.au"
    timeout_seconds: int = 300


class ResolveEmbedRequest(BaseModel):
    embed_url: str
    timeout_seconds: int = 30


@router.get("/status")
async def get_browser_status() -> dict[str, Any]:
    """Return diagnostic status of Camoufox stealth browser and persistent session."""
    service = get_camoufox_service()
    return service.get_status()


async def _run_login(target_url: str, timeout_seconds: int) -> None:
    """Background task: runs interactive_login and stores outcome in _login_job."""
    global _login_job
    service = get_camoufox_service()
    try:
        result = await service.interactive_login(
            target_url=target_url,
            timeout_seconds=timeout_seconds,
        )
        _login_job["result"] = result
        _login_job["error"] = None
        if result.get("success"):
            from unisync.services.sync.submissions import sync_submissions
            asyncio.create_task(sync_submissions())
    except Exception as exc:
        logger.error("Camoufox interactive login failed (%s)", type(exc).__name__)
        _login_job["error"] = type(exc).__name__
        _login_job["result"] = None
    finally:
        _login_job["running"] = False


@router.post("/login")
async def trigger_interactive_login(req: LoginRequest = LoginRequest()) -> dict[str, Any]:
    """Fire Camoufox headful login window in the background and return immediately.

    The browser window opens on the user's desktop. Poll GET /browser/login/status
    to know when authentication completes.
    """
    global _login_task
    service = get_camoufox_service()
    if not service.is_installed():
        raise HTTPException(status_code=503, detail="Camoufox browser binary is not installed.")

    if _login_job["running"]:
        return {"status": "already_running", "message": "Camoufox login window is already open."}

    _login_job["running"] = True
    _login_job["result"] = None
    _login_job["error"] = None

    # Fire and forget — the browser window opens on the desktop
    _login_task = asyncio.create_task(_run_login(req.target_url, req.timeout_seconds))

    return {
        "status": "started",
        "message": "Camoufox login window is opening on your desktop. Log in with Okta SSO, then come back here.",
    }


@router.get("/login/status")
async def get_login_status() -> dict[str, Any]:
    """Poll this to check if the background Camoufox login has completed."""
    if _login_job["running"]:
        return {"status": "running", "message": "Login window is open — complete Okta SSO in the browser."}
    if _login_job["error"]:
        return {"status": "error", "error": _login_job["error"]}
    if _login_job["result"]:
        return {"status": "done", **_login_job["result"]}
    return {"status": "idle", "message": "No login in progress."}


@router.post("/refresh")
async def refresh_browser_session() -> dict[str, Any]:
    """Silently run headless Camoufox with the persistent profile to refresh session cookies."""
    service = get_camoufox_service()
    if not service.is_installed():
        raise HTTPException(status_code=503, detail="Camoufox browser binary is not installed.")
    try:
        result = await service.background_sync_session()
        if result.get("authenticated"):
            from unisync.services.sync.submissions import sync_submissions
            asyncio.create_task(sync_submissions())
        elif result.get("login_required"):
            # An expired persistent session requires user SSO, not a legacy bridge tab.
            await trigger_interactive_login()
            result["login_required"] = True
        return result
    except Exception as exc:
        logger.error("Camoufox background refresh failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Session refresh unavailable; retry later.")


async def maintain_canvas_session(interval_seconds: int = 600) -> None:
    """Keep the browser session alive; trigger full sync only after auth recovery.

    A healthy 10-minute session probe is NOT a course-content change. Prior
    behaviour forced a full native sync after every successful probe, causing
    repeated Canvas requests and needless browser churn. Profile creation can
    also happen after startup; do not abandon maintenance forever when it does.
    """
    service = get_camoufox_service()
    if not service.get_status().get("installed"):
        return
    prompted = False
    while True:
        delay = interval_seconds
        try:
            if not service.get_status().get("has_persistent_profile"):
                # First-time login can occur after the server starts. Check
                # again without opening an unsolicited login window.
                delay = min(interval_seconds, 60)
            else:
                result = await service.background_sync_session()
                if result.get("authenticated"):
                    prompted = False
                    # Only a previously blocked sync should be rescheduled.
                    # Do not turn routine session keepalive into full resync.
                    try:
                        from unisync.services.sync.native_sync import NativeSyncState

                        state = NativeSyncState()
                        if (
                            state.data.get("login_required")
                            or state.data.get("last_run_status") in ("login_required", "auth_unknown")
                        ):
                            state.data["login_required"] = False
                            state.data["next_run_at"] = 0.0
                            state.save()
                    except Exception as exc:
                        logger.info("Deferred sync recovery scheduling unavailable (%s)", type(exc).__name__)
                elif result.get("login_required") and not prompted:
                    await trigger_interactive_login()
                    prompted = True
                elif not result.get("login_required"):
                    delay = min(interval_seconds, 60)
        except Exception as exc:
            # Temporary failure must not destroy the checkpoint or force SSO.
            logger.info("Canvas session maintenance unavailable (%s)", type(exc).__name__)
            delay = min(interval_seconds, 60)

        # Flush resident session changes without logging or exporting cookies.
        while delay > 0:
            pause = min(30, delay)
            await asyncio.sleep(pause)
            delay -= pause
            try:
                await service.flush()
            except Exception as exc:
                logger.info("Periodic session checkpoint unavailable (%s)", type(exc).__name__)


async def shutdown_browser(permanent: bool = False) -> None:
    if _login_task is not None and not _login_task.done():
        _login_task.cancel()
        try:
            await _login_task
        except asyncio.CancelledError:
            pass
    await get_camoufox_service().close(permanent=permanent)


@router.post("/close")
async def close_browser() -> dict[str, Any]:
    """Checkpoint and close the resident browser for maintenance or restart."""
    await shutdown_browser()
    return {"status": "closed"}


@router.post("/resolve-embed")
async def resolve_echo360_embed(req: ResolveEmbedRequest) -> dict[str, Any]:
    """Autonomously resolve a Canvas Echo360 embedded LTI player URL using Camoufox."""
    service = get_camoufox_service()
    if not service.is_installed():
        raise HTTPException(status_code=503, detail="Camoufox browser binary is not installed.")
    try:
        result = await service.resolve_echo360_embed(
            embed_url=req.embed_url,
            timeout_seconds=req.timeout_seconds,
        )
        return result
    except Exception as exc:
        logger.error("Echo360 embed resolution failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Embed resolution unavailable; retry later.")
