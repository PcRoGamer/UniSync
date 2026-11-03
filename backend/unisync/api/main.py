"""UniSync - FastAPI application entry point.

Mounts routers for:
- Canvas & Echo360 sync engine
- WebSocket progress broadcaster
- Lecture video streaming and syllabus
- EdStem course sync & snapshot ingestion
- University Email monitor (IMAP & Microsoft Graph)
- Academic deadlines & Todos
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from unisync.api.edstem import (
    router as edstem_router,
    settings_compat_router as edstem_settings_router,
    lectures_compat_router as edstem_lectures_router,
)
from unisync.api.sync_engine import router as sync_engine_router
from unisync.api.sync_ws import router as sync_ws_router
from unisync.api.lectures import router as lectures_router
from unisync.api.email_monitor import router as email_monitor_router
from unisync.api.todos import router as todos_router
from unisync.api.inventory import router as inventory_router
from unisync.api.dashboard import router as dashboard_router
from unisync.api.notebooklm import router as notebooklm_router
from unisync.api.browser import router as browser_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("unisync")
# HTTPX's routine request logs include signed attachment URLs and query tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle startup and shutdown handler."""
    logger.info("Starting UniSync server...")
    # Trigger background auto-sync for configured EdStem courses on startup
    try:
        from unisync.api.edstem import sync_all_configured_edstem_courses
        import asyncio
        asyncio.create_task(asyncio.to_thread(sync_all_configured_edstem_courses))
        logger.info("Triggered background EdStem course ingestion on startup")
    except Exception as ed_start_err:
        logger.debug("Failed spawning startup EdStem sync task: %s", ed_start_err)

    # Auto-trigger on-disk academic boilerplate scrubber in background
    try:
        from unisync.services.disk_cleaner import run_background_disk_scrubber
        run_background_disk_scrubber(delay_sec=1.0)
        logger.info("Auto-triggered on-disk academic boilerplate scrubber in background")
    except Exception as scrub_err:
        logger.debug("Failed spawning background disk scrubber: %s", scrub_err)
    # Keep the real browser session warm and restore its session cookies at startup.
    import asyncio
    from unisync.api.browser import maintain_canvas_session
    from unisync.api.sync_engine import native_sync_scheduler

    maintenance = asyncio.create_task(maintain_canvas_session())
    # Startup + periodic Camoufox-native discovery/transcript sync.
    native_sync = asyncio.create_task(native_sync_scheduler())
    try:
        yield
    finally:
        for task in (maintenance, native_sync):
            task.cancel()
        for task in (maintenance, native_sync):
            try:
                await task
            except asyncio.CancelledError:
                pass
        from unisync.api.browser import shutdown_browser
        await shutdown_browser(permanent=True)
        logger.info("Shutting down UniSync server...")


app = FastAPI(
    title="UniSync API",
    description="Universal University Sync Platform for Echo360, Canvas, EdStem, and Email",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for local web frontend and browser userscript
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API Routers
app.include_router(sync_engine_router, prefix="/api/v1/sync", tags=["sync"])
app.include_router(sync_ws_router, prefix="/api/v1/sync", tags=["sync_ws"])
app.include_router(lectures_router, prefix="/api/v1/lectures", tags=["lectures"])
app.include_router(edstem_router, prefix="/api/v1", tags=["edstem"])
app.include_router(edstem_settings_router, prefix="/api/v1", tags=["edstem-compat"])
app.include_router(edstem_lectures_router, prefix="/api/v1", tags=["edstem-compat"])
app.include_router(email_monitor_router)
app.include_router(todos_router)
app.include_router(inventory_router, prefix="/api/v1/sync", tags=["inventory"])
app.include_router(dashboard_router)
app.include_router(notebooklm_router, prefix="/api/v1", tags=["notebooklm"])
app.include_router(browser_router, prefix="/api/v1", tags=["browser"])


@app.get("/health", tags=["system"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "unisync", "version": "0.1.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("unisync.api.main:app", host="127.0.0.1", port=8000, reload=False)
