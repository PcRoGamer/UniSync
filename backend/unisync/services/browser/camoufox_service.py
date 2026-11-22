"""Camoufox Anti-Detect Browser Automation Service for UniSync.

This module provides a stealth, persistent-session browser service based on Camoufox
(a C++ engine-level anti-detect build of Firefox). It handles:
  1. Interactive 1-click Okta SSO authentication into Canvas LMS & Echo360.
  2. Persistent browser contexts (cookies, localStorage) stored on disk.
  3. Headless background cookie harvesting to feed UniSync's fast HTTP API clients.
  4. Autonomous LTI embed resolution (e.g. BLAW Echo360 seminars).
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ensure Windows on ARM64 compatibility:
# Camoufox releases win.x86_64 binaries which run under Windows 11 ARM64 Prism emulation.
# We map 'arm64' and 'aarch64' to 'x86_64' before importing Camoufox to prevent architecture rejection.
if sys.platform == "win32":
    try:
        import camoufox.pkgman as _pkgman
        _pkgman.ARCH_MAP["arm64"] = "x86_64"
        _pkgman.ARCH_MAP["aarch64"] = "x86_64"
    except ImportError:
        pass


class CamoufoxService:
    """Manages persistent anti-detect Camoufox browser sessions and cookie harvesting."""

    def __init__(self, profile_dir: Optional[Path] = None):
        if profile_dir is None:
            from unisync.core.path_service import get_path_service
            self.profile_dir = get_path_service().get_user_root() / "browser_profile" / "camoufox"
        else:
            self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_lock = asyncio.Lock()
        self._last_verified_at = None
        self._session_path = self.profile_dir / "unisync-session.json"
        self._identity_path = self.profile_dir / "unisync-identity.json"
        self._manager = None
        self._live_context = None
        self._headless = True
        self._context_dead = False
        self._canvas_page = None
        self._session_verified = False
        self._last_saved_at = 0.0
        self._launch_count = 0
        self._recovery_source = "profile"
        self._session_storage = {}
        self._shutting_down = False
        self._needs_navigation = False

    def is_installed(self) -> bool:
        """Check if camoufox is installed and browser executable exists."""
        try:
            from camoufox.pkgman import camoufox_path
            path = camoufox_path(download_if_missing=False)
            return path is not None and path.exists()
        except Exception:
            return False

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic status of Camoufox installation and profile."""
        installed = self.is_installed()
        has_profile = self.profile_dir.exists() and any(self.profile_dir.iterdir())
        
        # Report counts only; session values must never appear in diagnostics.
        from unisync.api.sync_engine import global_cookie_jar, sync_metadata
        
        return {
            "installed": installed,
            "profile_dir": str(self.profile_dir),
            "has_persistent_profile": has_profile,
            "cookies_in_memory": len(global_cookie_jar),
            "canvas_auth_status": sync_metadata.get("canvas_auth_status", "unknown"),
            "echo360_auth_status": sync_metadata.get("echo360_auth_status", "unknown"),
            "session_checkpoint_exists": self._session_path.exists(),
            "last_verified_at": self._last_verified_at,
            "profile_busy": self._profile_lock.locked(),
            "resident_browser": self._live_context is not None and not self._context_dead,
            "browser_launch_count": self._launch_count,
            "last_checkpoint_at": self._last_saved_at or None,
            "recovery_source": self._recovery_source,
        }

    @staticmethod
    def _write_private(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(".tmp")
        # Do not truncate the previous checkpoint until the new one is complete.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    @staticmethod
    def _identity_config(options: dict) -> dict:
        env = options["env"]
        keys = sorted((k for k in env if k.startswith("CAMOU_CONFIG_")),
                      key=lambda k: int(k.rsplit("_", 1)[1]))
        return json.loads("".join(env[k] for k in keys))

    def _launch_options(self, headless: bool) -> dict:
        """Reuse identity; migrate device fields while refreshing browser-version fields."""
        from camoufox.pkgman import installed_verstr
        from camoufox.utils import launch_options
        version = installed_verstr()
        identity = None
        try:
            identity = json.loads(self._identity_path.read_text(encoding="utf-8"))
            options = identity["options"]
            if not isinstance(options["env"], dict) or not isinstance(options["firefox_user_prefs"], dict):
                raise ValueError("Invalid identity")
            if identity["version"] != version:
                config = self._identity_config(options)
                for key in ("navigator.userAgent", "navigator.appVersion", "navigator.buildID"):
                    config.pop(key, None)
                options = launch_options(config=config, headless=False, enable_cache=True,
                                         os="windows" if sys.platform == "win32" else None,
                                         i_know_what_im_doing=True)
        except (OSError, ValueError, KeyError, TypeError):
            options = launch_options(headless=False, enable_cache=True,
                                     os="windows" if sys.platform == "win32" else None)
        options["env"] = {k: v for k, v in options["env"].items()
                          if k.startswith("CAMOU_CONFIG_") or k == "FONTCONFIG_FILE"}
        persisted = {"version": version, "options": options}
        if identity != persisted:
            if identity:
                self._write_private(self._identity_path.with_suffix(".bak"), identity)
            self._write_private(self._identity_path, persisted)
        options["env"] = {**os.environ, **options["env"]}
        options.update(headless=headless, user_data_dir=str(self.profile_dir), no_viewport=True)
        options["firefox_user_prefs"].update({
            "browser.privatebrowsing.autostart": False,
            "privacy.sanitize.sanitizeOnShutdown": False,
            "privacy.clearOnShutdown.cookies": False,
            "privacy.clearOnShutdown.offlineApps": False,
            "privacy.clearOnShutdown_v2.cookiesAndStorage": False,
            "network.cookie.lifetimePolicy": 0,
            # Never play sound on the user's PC: lecture downloads and transcript
            # ingest start the Echo360 player in this browser.
            "media.volume_scale": "0.0",
        })
        return options

    def _load_snapshot(self, record_source: bool = False) -> dict | None:
        for path in (self._session_path, self._session_path.with_suffix(".bak")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data.get("cookies"), list) or not isinstance(data.get("origins", []), list):
                    continue
                if any(not isinstance(c, dict) or not all(isinstance(c.get(k), str)
                       for k in ("domain", "name", "value", "path"))
                       or not isinstance(c.get("expires", -1), (int, float)) for c in data["cookies"]):
                    continue
                if record_source:
                    self._recovery_source = "checkpoint" if path == self._session_path else "backup"
                return data
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return None

    async def _restore(self, context) -> None:
        saved = self._load_snapshot(record_source=True)
        if not saved:
            return
        current = {(c["domain"], c["name"], c.get("path", "/")) for c in await context.cookies()}
        cookies = [c for c in saved["cookies"]
                   if (c.get("expires", -1) == -1 or c["expires"] > time.time())
                   and (c["domain"], c["name"], c.get("path", "/")) not in current]
        if cookies:
            await context.add_cookies(cookies)
        # Full replacement is recovery-only: never overwrite a healthy current profile.
        if saved.get("origins") and await self._probe(context) is False:
            live = [c for c in saved["cookies"]
                    if c.get("expires", -1) == -1 or c["expires"] > time.time()]
            await context.set_storage_state({"cookies": live, "origins": saved["origins"]})
            self._recovery_source = "full-checkpoint"
        self._session_storage = saved.get("session_storage") or {}
        canvas_origin = "https://canvas.lms.unimelb.edu.au"
        values = self._session_storage.get(canvas_origin)
        if isinstance(values, dict) and values:
            # Restore only observed Canvas tab state, only into an empty sessionStorage.
            script = """(() => {
                const saved = __STATE__;
                if (location.origin === 'https://canvas.lms.unimelb.edu.au' && sessionStorage.length === 0) {
                    for (const [key, value] of Object.entries(saved)) sessionStorage.setItem(key, value);
                }
            })();""".replace("__STATE__", json.dumps(values))
            await context.add_init_script(script=script)
        await self._seed_cookie_jar(context)

    async def _seed_cookie_jar(self, context) -> None:
        # Mirror restored browser cookies into the in-memory jar.
        #
        # _restore only patches the browser context. The backend HTTP
        # clients read global_cookie_jar via get_cookies_for_domain, so
        # without this the session looks dead even when the restored
        # checkpoint still carries valid cookies.
        from unisync.api.sync_engine import global_cookie_jar

        try:
            live = await context.cookies()
        except Exception:
            return
        if not live:
            return
        global_cookie_jar.clear()
        global_cookie_jar.update(
            {
                (cookie['domain'], cookie['name']): cookie['value']
                for cookie in live
                if cookie.get('name') and cookie.get('value')
            }
        )

    def _connected(self) -> bool:
        if self._live_context is None or self._context_dead:
            return False
        browser = self._live_context.browser
        return browser is None or browser.is_connected()

    async def _close_locked(self) -> None:
        manager = self._manager
        self._manager = self._live_context = self._canvas_page = None
        self._context_dead = False
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception as exc:
                logger.info("Browser close unavailable (%s)", type(exc).__name__)

    async def _ensure_context_locked(self, headless: bool):
        from camoufox.async_api import AsyncCamoufox
        if self._shutting_down:
            raise RuntimeError("Browser service is shutting down")
        # A visible resident context can serve background work. Only interactive
        # login needs a transition from headless to visible.
        if self._connected() and (headless or not self._headless):
            return self._live_context
        if self._connected() and self._session_verified:
            await self._persist_state(self._live_context)
        await self._close_locked()
        options = await asyncio.to_thread(self._launch_options, headless)
        manager = AsyncCamoufox(persistent_context=True, from_options=options)
        context = await manager.__aenter__()
        self._manager, self._live_context = manager, context
        self._headless, self._context_dead = headless, False
        self._session_verified = False
        self._launch_count += 1
        self._needs_navigation = True
        def closed(*_):
            if self._live_context is context:
                self._context_dead = True
        context.on("close", closed)
        try:
            await self._restore(context)
        except BaseException:
            await self._close_locked()
            raise
        return context

    @asynccontextmanager
    async def _context(self, headless: bool):
        # Single owner for launch, API operations, recovery and checkpoint writes.
        async with self._profile_lock:
            context = await self._ensure_context_locked(headless)
            try:
                yield context
            finally:
                if self._connected() and self._session_verified and time.time() - self._last_saved_at >= 15:
                    try:
                        await self._persist_state(context)
                    except Exception as exc:
                        if type(exc).__name__ == "TargetClosedError":
                            self._context_dead = True
                        logger.info("Deferred browser checkpoint unavailable (%s)", type(exc).__name__)

    async def _get_canvas_page(self, context):
        if self._canvas_page is None or self._canvas_page.is_closed():
            self._canvas_page = next((p for p in context.pages if not p.is_closed()), None)
            if self._canvas_page is None:
                self._canvas_page = await context.new_page()
        return self._canvas_page

    async def _warm_canvas_page(self, context) -> None:
        """Keep a real app tab alive so its normal in-page refresh logic can run."""
        page = await self._get_canvas_page(context)
        current = urlparse(page.url)
        if self._needs_navigation or current.hostname != "canvas.lms.unimelb.edu.au" or current.path.startswith('/login'):
            try:
                await page.goto("https://canvas.lms.unimelb.edu.au", wait_until="domcontentloaded", timeout=30000)
                self._needs_navigation = False
            except Exception as exc:
                logger.info("Canvas app tab unavailable (%s)", type(exc).__name__)

    async def flush(self) -> None:
        async with self._profile_lock:
            if self._connected() and self._session_verified:
                await self._persist_state(self._live_context)

    async def close(self, permanent: bool = False) -> None:
        self._shutting_down = permanent
        async with self._profile_lock:
            try:
                if self._connected() and self._session_verified:
                    await self._persist_state(self._live_context)
            finally:
                await self._close_locked()

    def authentication_lost(self) -> None:
        from unisync.api.sync_engine import sync_metadata
        self._session_verified = False
        sync_metadata.update(canvas_auth_status="expired", auth_status="expired",
                             canvas_auth_message="Canvas requires authentication")

    async def _probe(self, context) -> bool | None:
        """Only Canvas can confirm auth. Network errors/HTML are not expiry proof."""
        try:
            response = await context.request.get(
                "https://canvas.lms.unimelb.edu.au/api/v1/users/self/profile",
                timeout=10000, max_redirects=0)
            try:
                if response.status == 401:
                    return False
                if response.status == 200:
                    payload = await response.json()
                    if isinstance(payload, dict) and payload.get("id"):
                        return True
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "").lower()
                    if any(s in location for s in ("/login", "okta", "/saml", "/sso")):
                        return False
                return None
            finally:
                await response.dispose()
        except Exception as exc:
            logger.info("Canvas authentication check unavailable (%s)", type(exc).__name__)
            return None

    async def _mirror_cookies(self, context) -> list[dict]:
        from unisync.api.sync_engine import global_cookie_jar
        cookies = await context.cookies()
        # The resident browser is authoritative, including cookie removals.
        global_cookie_jar.clear()
        global_cookie_jar.update({(c["domain"], c["name"]): c["value"] for c in cookies})
        return cookies

    async def _persist_state(self, context) -> int:
        state = await context.storage_state(indexed_db=True)
        cookies = await self._mirror_cookies(context)
        # Browser storage_state includes local storage and IndexedDB as well as cookies.
        state["cookies"] = cookies
        page = self._canvas_page
        if page is not None and not page.is_closed() and urlparse(page.url).hostname == "canvas.lms.unimelb.edu.au":
            try:
                values = await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
                if isinstance(values, dict) and all(isinstance(v, str) for v in values.values()):
                    self._session_storage["https://canvas.lms.unimelb.edu.au"] = values
            except Exception as exc:
                logger.info("Canvas tab checkpoint unavailable (%s)", type(exc).__name__)
        previous = self._load_snapshot()
        if previous:
            self._write_private(self._session_path.with_suffix(".bak"), previous)
        state.update(schema=2, saved_at=time.time(), session_storage=self._session_storage)
        self._write_private(self._session_path, state)
        self._last_saved_at = state["saved_at"]
        return len(cookies)

    async def _checkpoint(self, context) -> int:
        from unisync.api.sync_engine import sync_metadata
        self._session_verified = True
        count = await self._persist_state(context)
        self._last_verified_at = time.time()
        sync_metadata.update(canvas_auth_status="authenticated", auth_status="authenticated",
                             canvas_auth_message="Verified resident Canvas session")
        return count

    async def _wait_for_auth(self, context, page, timeout_seconds: float) -> bool | None:
        last = None
        async def wait():
            nonlocal last
            while True:
                if urlparse(page.url).hostname == "canvas.lms.unimelb.edu.au":
                    last = await self._probe(context)
                    if last is True:
                        return True
                await asyncio.sleep(2)
        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except asyncio.TimeoutError:
            return last

    async def interactive_login(self, target_url: str = "https://canvas.lms.unimelb.edu.au",
                                timeout_seconds: int = 300) -> dict[str, Any]:
        """Reuse the saved session before requesting interactive SSO."""
        async with self._context(headless=False) as context:
            if await self._probe(context) is not True:
                page = await self._get_canvas_page(context)
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                if await self._wait_for_auth(context, page, timeout_seconds) is not True:
                    raise TimeoutError("Canvas did not confirm authenticated login before timeout")
            await self._warm_canvas_page(context)
            count = await self._checkpoint(context)
            return {"success": True, "cookies_count": count,
                    "message": "Canvas authentication verified and session saved."}

    async def background_sync_session(self, timeout_seconds: int = 45) -> dict[str, Any]:
        """Restore session cookies, validate, then allow silent SSO recovery."""
        from unisync.api.sync_engine import sync_metadata
        async with self._context(headless=True) as context:
            authenticated = await self._probe(context)
            if authenticated is not True:
                page = await self._get_canvas_page(context)
                started = time.monotonic()
                try:
                    await page.goto("https://canvas.lms.unimelb.edu.au", wait_until="domcontentloaded",
                                    timeout=min(timeout_seconds, 30) * 1000)
                    recovered = await self._wait_for_auth(
                        context, page, max(1, timeout_seconds - (time.monotonic() - started)))
                    if recovered is not None:
                        authenticated = recovered
                except Exception as exc:
                    logger.info("Silent SSO recovery unavailable (%s)", type(exc).__name__)
                    authenticated = None
            if authenticated is True:
                await self._warm_canvas_page(context)
                count = await self._checkpoint(context)
                return {"authenticated": True, "cookies_count": count,
                        "login_required": False, "message": "Canvas session verified and saved."}
            expired = authenticated is False
            if expired:
                self.authentication_lost()
            sync_metadata.update(canvas_auth_status="expired" if expired else "unknown",
                                 canvas_auth_message="Sign-in required" if expired else "Canvas temporarily unavailable")
            return {"authenticated": False, "login_required": expired,
                    "message": "Interactive sign-in required." if expired else "Could not verify Canvas; retry later."}

    async def resolve_echo360_embed(self, embed_url: str, timeout_seconds: int = 30) -> dict[str, Any]:
        """LTI resolution shares the profile lock and preserved login state."""
        captured_urls: list[str] = []
        async with self._context(headless=True) as context:
            page = await context.new_page()
            def handle_request(req):
                url = req.url
                if "echo360.net.au" in url and any(k in url for k in ("syllabus", "classroom", "media", "hls")):
                    captured_urls.append(url)
            page.on("request", handle_request)
            try:
                await page.goto(embed_url, timeout=timeout_seconds * 1000)
                await asyncio.sleep(5)
                if await self._probe(context) is True:
                    await self._checkpoint(context)
            finally:
                page.remove_listener("request", handle_request)
                final_url = page.url
                await page.close()
            return {"success": bool(captured_urls), "captured_urls": captured_urls, "final_url": final_url}


_SERVICE_INSTANCE: Optional[CamoufoxService] = None


def get_camoufox_service() -> CamoufoxService:
    """Singleton getter for CamoufoxService."""
    global _SERVICE_INSTANCE
    if _SERVICE_INSTANCE is None:
        _SERVICE_INSTANCE = CamoufoxService()
    return _SERVICE_INSTANCE
