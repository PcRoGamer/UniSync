import asyncio
import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from fastapi import APIRouter, Body, BackgroundTasks, File, Form, UploadFile
import logging

logger = logging.getLogger(__name__)

# P0: bound concurrency for media downloads; protect globals from async races
_sync_lock = asyncio.Lock()          # guards sync_status + global_cookie_jar mutations
_download_sem = asyncio.Semaphore(1)  # one 105MB video at a time (native-code stability)

# P1-10: TTL cache for existing Echo360 IDs — avoids O(n) disk scan on every 15s poll
# Oracle fix: cache keyed by include_failed so True/False queries get separate entries
_CACHE_TTL = 30.0
_existing_ids_cache: dict[bool, dict[str, Any]] = {
    False: {"ids": set(), "at": 0.0},
    True: {"ids": set(), "at": 0.0},
}

_STARTUP_TIME = time.time()

router = APIRouter()
_bridge_commands: list[dict[str, Any]] = []
_bridge_command: dict[str, Any] | None = None  # compat shim (last item) — not used for dispatch
_bridge_results: list[dict[str, Any]] = []
_bridge_tabs: dict[str, dict[str, Any]] = {}

@router.post("/bridge/results")
async def save_bridge_result(body: dict[str, Any] = Body(...)):
    _bridge_results.append({"received_at": time.time(), **body})
    del _bridge_results[:-500]
    return {"saved": True}

@router.get("/bridge/results")
async def get_bridge_results():
    return {"results": _bridge_results}

@router.get("/bridge/tabs")
async def get_bridge_tabs():
    now = time.time()
    tabs = [tab for tab in _bridge_tabs.values() if now - tab.get("last_seen", 0) < 90]
    # sort by last_seen desc so the freshest, most descriptive tab title surfaces first
    tabs.sort(key=lambda x: x.get("last_seen", 0), reverse=True)
    return {"tabs": tabs, "pending_commands": len(_bridge_commands)}

@router.post("/bridge/command")
async def queue_bridge_command(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    global _bridge_command
    entry = {"command": str(body.get("command", "")), "target": str(body.get("target", "")), "url": str(body.get("url", "")), "id": str(time.time()), "queued_at": time.time()}
    _bridge_commands.append(entry)
    # keep last 20 so a burst of blaw commands cannot starve diagnostics
    del _bridge_commands[:-20]
    _bridge_command = entry
    logger.info("Bridge queue: queued=%s target=%s id=%s (depth=%d)", entry["command"][:80], entry["target"] or "-", entry["id"], len(_bridge_commands))
    return {"queued": True, **entry, "depth": len(_bridge_commands)}

@router.get("/bridge/command")
async def take_bridge_command(tab: str = "", tab_id: str = "") -> dict[str, Any]:
    global _bridge_command
    if not _bridge_commands:
        return {}
    # FIFO: deliver the first command whose target matches this tab, otherwise empty
    for idx, cmd in enumerate(_bridge_commands):
        tgt = cmd.get("target") or ""
        if tgt and tgt not in tab and tgt != tab_id:
            continue
        # deliver and consume
        claimed = _bridge_commands.pop(idx)
        _bridge_command = None if not _bridge_commands else _bridge_commands[-1]
        logger.info("Bridge queue: claimed=%s target=%s tab=%s id=%s", claimed["command"][:80], tgt or "-", tab[:80], claimed["id"])
        return claimed
    return {}


# ── State-Diffing Engine (Zero-Compute Delta Synchronization) ───────────────
# Mirrors the lightweight state snapshot model: checks incoming syllabus/module
# trees against local snapshot before allocating disk, network, or LLM compute.
# Re-running on unchanged courses takes ~0ms and spends 0 LLM tokens.

def _state_file() -> Path:
    from unisync.core.path_service import get_path_service
    path = get_path_service().get_user_root() / "sync_state" / "sync_delta_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _canvas_files_file() -> Path:
    """Path to the registered Canvas files manifest."""
    from unisync.core.path_service import get_path_service
    path = get_path_service().get_user_root() / "libraries" / "canvas_files.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_canvas_files() -> dict[str, Any]:
    """Loads the canvas_files.json manifest safely."""
    file = _canvas_files_file()
    if not file.exists():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_canvas_files(data: dict[str, Any]) -> None:
    """Saves the canvas_files.json manifest atomically."""
    from unisync.core.file_io import atomic_write_json
    atomic_write_json(_canvas_files_file(), data)


def _load_sync_state() -> dict[str, Any]:
    file = _state_file()
    if not file.exists():
        return {"courses": {}, "recent_diffs": []}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return {"courses": {}, "recent_diffs": []}


def _save_sync_state(state: dict[str, Any]) -> None:
    from unisync.core.file_io import atomic_write_json
    if len(state.get("recent_diffs", [])) > 50:
        state["recent_diffs"] = state["recent_diffs"][-50:]
    atomic_write_json(_state_file(), state)


def _record_diff(state: dict[str, Any], course_name: str, kind: str, message: str) -> None:
    from datetime import datetime, timezone
    state.setdefault("recent_diffs", []).append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "course": course_name,
        "kind": kind,
        "message": message,
    })


# P2-12: helper to extract a sortable lecture date from a syllabus item.
# Tries nested lesson→lesson→timing→start, then item-level lecture_date, then "".
def _lecture_date(item: dict) -> str:
    """Return a date string suitable for sorting syllabus items most-recent-first.

    Probes the Echo360 payload structure in priority order:
      1. item.lesson.lesson.timing.start   (canonical nested path)
      2. item.lesson.timing.start          (flattened variant)
      3. item.lecture_date                  (pre-extracted by caller)
      4. ""                                (treat as oldest when absent)
    """
    les_obj = item.get("lesson") or {}
    les_inner = les_obj.get("lesson") or les_obj
    return (
        (les_inner.get("timing") or {}).get("start", "")
        or item.get("lecture_date", "")
        or ""
    )


def _cues_to_vtt(cues: list[dict[str, Any]]) -> str:
    """Converts Echo360 JSON cue arrays into standard WebVTT subtitles format."""
    def _ms_to_vtt_timestamp(ms: int) -> str:
        h = ms // 3600000
        ms %= 3600000
        m = ms // 60000
        ms %= 60000
        s = ms // 1000
        ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    lines = ["WEBVTT", ""]
    for cue in cues:
        text = str(cue.get("content") or cue.get("text") or "").strip()
        if not text:
            continue
        start_val = cue.get("startMs")
        if start_val is None:
            start_val = int(float(cue.get("start", 0)) * 1000)
        else:
            start_val = int(start_val)

        end_val = cue.get("endMs")
        if end_val is None:
            end_val = int(float(cue.get("end", 0)) * 1000)
        else:
            end_val = int(end_val)

        start = _ms_to_vtt_timestamp(start_val)
        end = _ms_to_vtt_timestamp(end_val)
        speaker = cue.get("speaker", "").strip()
        lines.append(f"{start} --> {end}")
        if speaker:
            lines.append(f"<v {speaker}>{text}")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


# Global in-memory cookie jar populated by the Thin Bridge userscript
global_cookie_jar = {}
sync_status = {
    "is_syncing": False,
    "progress": {"message": "", "done": 0, "total": 0},
    "log": []
}

def _get_initial_last_synced() -> str | None:
    """Extracts latest update timestamp from manifest or delta state on startup."""
    try:
        from unisync.core.path_service import get_path_service
        mf = get_path_service().get_user_root() / "downloads_manifest.json"
        if mf.exists():
            data = json.loads(mf.read_text(encoding="utf-8"))
            if data.get("updated_at"):
                return str(data["updated_at"])
    except Exception:
        pass
    return None


def _get_latest_userscript_version() -> str:
    """Reads the current canonical userscript version from disk for bridge negotiation."""
    try:
        from pathlib import Path
        import re
        candidates = [
            Path(__file__).resolve().parents[3] / "userscript" / "echo360-connector.user.js",
            Path(__file__).resolve().parent.parent / "assets" / "echo360_userscript" / "echo360-connector.user.js",
        ]
        for p in candidates:
            if p.exists():
                content = p.read_text(encoding="utf-8")
                m = re.search(r"@version\s+(\S+)", content)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return "4.0.3"


sync_metadata: dict[str, Any] = {
    "last_synced_at": _get_initial_last_synced(),
    "last_bridge_ping": 0.0,
    "bridge_tab": "",
    "bridge_version": "",
    "auth_status": "unknown",
    "auth_message": "",
    # P2-19: per-platform auth so Canvas LMS and Echo360 sessions are tracked
    # independently. The single global auth_status above is derived/compat — a
    # heartbeat from any UniSync page clobbered it, so "authenticated" never
    # meant "Canvas LMS is logged in". Separate keys fix that.
    "canvas_auth_status": "unknown",
    "canvas_auth_message": "",
    "echo360_auth_status": "unknown",
    "echo360_auth_message": "",
}

def get_computed_sync_status() -> dict[str, Any]:
    """Computes comprehensive sync status, bridge connectivity, auth state, and asset stats."""
    from datetime import datetime
    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    is_syncing = sync_status.get("is_syncing", False)
    bridge_active = (now - sync_metadata["last_bridge_ping"]) < 90.0 if sync_metadata["last_bridge_ping"] > 0 else False
    auth_status = sync_metadata["auth_status"]
    # P2-19: expose per-platform auth separately
    canvas_auth_status = sync_metadata.get("canvas_auth_status", "unknown")
    echo360_auth_status = sync_metadata.get("echo360_auth_status", "unknown")
    canvas_auth_message = sync_metadata.get("canvas_auth_message", "")

    # Inspect lecture stats from disk
    latest_lecture_date = None
    lecture_count = 0
    try:
        from unisync.core.path_service import get_path_service
        vtt_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
        if vtt_dir.exists():
            import re
            vtts = list(vtt_dir.glob("*.vtt"))
            lecture_count = len(vtts)
            dates = []
            for f in vtts:
                m = re.search(r'(\d{4}-\d{2}-\d{2})', f.name)
                if m:
                    dates.append(m.group(1))
            if dates:
                latest_lecture_date = max(dates)
    except Exception:
        pass

    # Inspect total manifest assets
    total_assets = 0
    try:
        from unisync.core.download_registry import DownloadRegistry
        reg = DownloadRegistry.get_instance()
        total_assets = len(reg._items)
    except Exception:
        pass

    # Calculate pending lectures from syllabus schedule
    pending_lectures_count = 0
    pending_lectures_dates = []
    try:
        from unisync.core.path_service import get_path_service
        syl_file = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
        vtt_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
        if not syl_file.exists():
            fallback = Path("reference/scratch_echo360_payload.json")
            if fallback.exists():
                syl_file = fallback
        if syl_file.exists():
            syl_data = json.loads(syl_file.read_text(encoding="utf-8"))
            cutoff_date = latest_lecture_date or "1970-01-01"
            date_set = set()
            for sec in syl_data:
                syl = sec.get("syllabus") or []
                if isinstance(syl, list):
                    for entry in syl:
                        les = entry.get("lesson") or {}
                        inner = les.get("lesson") or les
                        lid = inner.get("id") or ""
                        # Synthetic BLAW_EMBED_ items are supplementary Canvas video embeds
                        # handled separately by embed resolver; their start timestamp is set to
                        # the scraper runtime, not a timetable class date. Skipping here matches
                        # lectures.py catalog line 1425.
                        if lid.startswith("BLAW_EMBED_"):
                            continue
                        timing = inner.get("timing") or {}
                        start = timing.get("start") or ""
                        if start:
                            d = start[:10]
                            if cutoff_date < d <= today_str:
                                # Check if already synced / downloaded on disk
                                safe_lid = re.sub(r"[^a-zA-Z0-9_\-]", "_", lid)
                                has_sub = (vtt_dir / f"{safe_lid}.vtt").exists() if lid else False
                                if not has_sub and (vtt_dir / f"{lid}.vtt").exists():
                                    has_sub = True
                                if has_sub or bool(entry.get("subtitles")):
                                    continue
                                pending_lectures_count += 1
                                date_set.add(d)
            pending_lectures_dates = sorted(list(date_set))
    except Exception as err:
        logger.debug("Failed checking pending syllabus lectures: %s", err)

    camoufox_status = {"installed": False, "has_persistent_profile": False}
    try:
        from unisync.services.browser import get_camoufox_service
        camoufox_status = get_camoufox_service().get_status()
    except Exception:
        pass

    if is_syncing:
        state = "syncing"
        state_label = "Syncing in Progress"
        state_desc = sync_status.get("progress", {}).get("message") or "Synchronizing university modules, lectures, and materials..."
    elif auth_status == "waiting_for_auth":
        state = "waiting_for_auth"
        state_label = "Waiting for LMS Login"
        if canvas_auth_status == "waiting_for_auth" and canvas_auth_message:
            state_desc = canvas_auth_message
        else:
            state_desc = sync_metadata.get("auth_message") or "University session not detected. Please log in to Canvas LMS / Echo360."
    elif not bridge_active:
        if camoufox_status.get("has_persistent_profile") and (canvas_auth_status == "authenticated" or camoufox_status.get("cookies_in_memory", 0) > 0):
            state = "camoufox_ready"
            state_label = "Camoufox Autonomous Sync Ready"
            state_desc = "Stealth browser session active. Background syncs ready without requiring open browser tabs."
        elif pending_lectures_count > 0:
            state = "waiting_for_tab"
            state_label = f"Waiting for Tab ({pending_lectures_count} New Lectures)"
            state_desc = f"Userscript idle — {pending_lectures_count} lecture(s) occurred since {latest_lecture_date}. Open Canvas LMS or Echo360 to start sync."
        else:
            state = "waiting_for_tab"
            state_label = "Waiting for Active LMS Tab"
            state_desc = "Userscript idle — open Canvas LMS or Echo360 in your browser, or login with Camoufox."
    elif pending_lectures_count > 0:
        state = "lectures_pending"
        state_label = f"{pending_lectures_count} Lectures Awaiting Sync"
        state_desc = f"Latest recording on disk is {latest_lecture_date}. {pending_lectures_count} newer lecture(s) occurred in class (up to {today_str}). Open Canvas Lecture Capture to download recordings."
    else:
        state = "up_to_date"
        state_label = "Up to Date"
        state_desc = "All enrolled courses and lecture recordings are up to date."

    return {
        "state": state,
        "state_label": state_label,
        "state_description": state_desc,
        "is_syncing": is_syncing,
        "bridge_active": bridge_active,
        "active_tab": sync_metadata.get("bridge_tab") or "",
        "auth_status": auth_status,
        "canvas_auth_status": canvas_auth_status,
        "canvas_auth_message": canvas_auth_message,
        "echo360_auth_status": echo360_auth_status,
        "echo360_auth_message": sync_metadata.get("echo360_auth_message") or "",
        "last_synced_at": sync_metadata.get("last_synced_at"),
        "latest_lecture_date": latest_lecture_date,
        "lecture_count": lecture_count,
        "total_assets": total_assets,
        "pending_lectures_count": pending_lectures_count,
        "pending_lectures_dates": pending_lectures_dates,
        "bridge_version": sync_metadata.get("bridge_version") or "",
        "latest_userscript_version": _get_latest_userscript_version(),
        "progress": sync_status.get("progress", {}),
        "camoufox": camoufox_status,
    }

def log_sync(message: str, done: int = 0, total: int = 0):
    """Append a progress message to the in-memory sync log.

    NOTE: log_sync is intentionally synchronous — it is only called from
    the request handler (sync_payload / _run_sync_pipeline) which already
    holds ``_sync_lock`` when mutating ``sync_status``, so the mutations
    below are safe without acquiring the lock again here.
    """
    logger.info(message)
    # P2-18: dedupe — suppress a broadcast when the new progress is byte-for-byte
    # identical to the previous one (same text + same done + same total). The
    # userscript drives a hybrid sync pass every ~8-15s and every pass re-logs
    # "Hybrid … Sync Complete!" even when nothing changed, which flooded the
    # WebSocket console and sync log with ~N identical no-op completions. Only a
    # genuinely different progress event deserves a push.
    prev = sync_status.get("progress") or {}
    if (
        prev.get("message") == message
        and prev.get("done") == done
        and prev.get("total") == total
    ):
        # Still record the terminal line once in the log if absent, but skip the
        # broadcast so the UI doesn't stack duplicate "Saved" entries.
        if not sync_status["log"] or sync_status["log"][-1] != message:
            sync_status["log"].append(message)
            if len(sync_status["log"]) > 100:
                sync_status["log"] = sync_status["log"][-100:]
        return
    sync_status["progress"] = {"message": message, "done": done, "total": total}
    sync_status["log"].append(message)
    # keep last 100
    if len(sync_status["log"]) > 100:
        sync_status["log"] = sync_status["log"][-100:]
    # P2-16: push to WebSocket subscribers — fire-and-forget
    # Throttle: only broadcast every 5th update or on completion to reduce
    # WS fan-out from 100+ async tasks → ~20 per sync run.
    if done % 5 == 0 or done == total or done == 0:
        _schedule_broadcast()


def _schedule_broadcast() -> None:
    """Fire-and-forget broadcast to SyncProgressBroadcaster.

    Safe to call from both sync and async contexts.  If no event loop is
    running (e.g. called from a plain thread), the broadcast is silently
    skipped — the next ``log_sync`` call will pick it up.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do_broadcast()))
    except RuntimeError:
        pass  # no running event loop — skip (sync thread path)


async def _do_broadcast() -> None:
    """Push current ``sync_status`` and computed state to all WebSocket subscribers."""
    from unisync.api.sync_ws import get_sync_broadcaster

    try:
        payload = dict(sync_status)
        payload["computed"] = get_computed_sync_status()
        payload["probes"] = get_all_probes()
        await get_sync_broadcaster().broadcast(payload)
    except Exception:
        logger.debug("P2-16: _do_broadcast failed", exc_info=True)

# P1-10: cache disk scan — avoids O(n) on every 15s poll.
# Returns cached set of existing Echo360 IDs if within TTL, otherwise
# rescans disk and updates the cache. Keeps include_failed param.
def _cached_existing_ids(include_failed: bool = False) -> set:
    # Oracle fix: cache keyed by include_failed — separate TTL per param value
    entry = _existing_ids_cache[include_failed]
    now = time.time()
    if now - entry["at"] < _CACHE_TTL:
        return entry["ids"]
    # TTL expired — rescan disk via the original helper (import locally to avoid circular dep)
    from unisync.api.lectures import _existing_echo360_ids
    ids = _existing_echo360_ids(include_failed=include_failed)
    entry["ids"] = ids
    entry["at"] = now
    return ids


_VTT_CACHE_TTL = 30.0
_vtt_cache: dict[str, Any] = {"count": 0, "at": 0.0}

def get_all_probes() -> dict[str, Any]:
    """Runs comprehensive diagnostic health probes across all 8 UniSync subsystems.
    
    Each probe reports:
    - status: 'ok' | 'warning' | 'error' | 'offline' | 'idle'
    - label: Human-readable status title
    - latency_ms: Execution time in milliseconds
    - details: Subsystem-specific diagnostic metrics
    - actions: Recommended quick actions for the UI
    """
    now = time.time()
    probes: dict[str, Any] = {}

    # 1. Backend Server & Process Health
    t0 = time.perf_counter()
    try:
        import platform, os
        uptime_sec = round(now - _STARTUP_TIME, 1)
        uptime_str = f"{int(uptime_sec // 3600)}h {int((uptime_sec % 3600) // 60)}m {int(uptime_sec % 60)}s" if uptime_sec > 60 else f"{int(uptime_sec)}s"
        probes["backend"] = {
            "status": "ok",
            "label": "FastAPI Service Operational",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "pid": os.getpid(),
                "python": platform.python_version(),
                "uptime": uptime_str,
                "uptime_seconds": uptime_sec,
                "host": "127.0.0.1:8000",
                "mode": "Local Daemon",
            },
            "actions": [{"id": "ping", "label": "Health Ping", "endpoint": "/health"}]
        }
    except Exception as exc:
        probes["backend"] = {"status": "error", "label": "Backend Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 2. Canvas LMS Session & Scraper
    t0 = time.perf_counter()
    try:
        canvas_status = sync_metadata.get("canvas_auth_status", "unknown")
        canvas_cookies = [k for k in global_cookie_jar if "canvas" in k or "_session_id" in k or "log_in_to" in k]
        from unisync.api.lectures import _load_libraries
        libs = _load_libraries()
        enrolled_count = len([lib for lib in libs.values() if lib.get("is_academic", True)])
        delta_state = _load_sync_state()
        tracked_courses = len(delta_state.get("courses", {}))
        
        is_ok = canvas_status == "authenticated" or len(canvas_cookies) > 0
        status_val = "ok" if is_ok else ("warning" if canvas_status == "waiting_for_auth" else "offline")
        label_val = "Session Authenticated" if is_ok else ("Okta SSO Required" if canvas_status == "waiting_for_auth" else "No Active Session")
        
        probes["canvas"] = {
            "status": status_val,
            "label": label_val,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "auth_status": canvas_status,
                "auth_message": sync_metadata.get("canvas_auth_message", ""),
                "cookies_cached": len(canvas_cookies),
                "enrolled_academic_courses": enrolled_count,
                "tracked_courses_in_delta": tracked_courses,
                "last_synced_at": sync_metadata.get("last_synced_at"),
            },
            "actions": [
                {"id": "open_canvas", "label": "Open Canvas ↗", "url": "https://canvas.lms.unimelb.edu.au"},
                {"id": "reprobe_canvas", "label": "Re-probe Session", "type": "reprobe"}
            ]
        }
    except Exception as exc:
        probes["canvas"] = {"status": "error", "label": "Canvas Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 3. Echo360 Lecture Capture Bridge
    t0 = time.perf_counter()
    try:
        echo_status = sync_metadata.get("echo360_auth_status", "unknown")
        if now - _vtt_cache["at"] < _VTT_CACHE_TTL:
            vtt_count = _vtt_cache["count"]
        else:
            import os
            from unisync.core.path_service import get_path_service
            vtt_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
            vtt_count = 0
            if vtt_dir.exists():
                try:
                    with os.scandir(vtt_dir) as it:
                        vtt_count = sum(1 for e in it if e.name.endswith(".vtt"))
                except Exception:
                    pass
            _vtt_cache["count"] = vtt_count
            _vtt_cache["at"] = now
        
        # Determine LTI URL
        lti_url = ""
        for lib in libs.values():
            if lib.get("lecture_capture_url"):
                lti_url = lib["lecture_capture_url"]
                break

        # Check existing cached lectures
        from unisync.core.download_registry import DownloadRegistry
        reg = DownloadRegistry.get_instance()
        echo_items = [it for it in reg.list_all() if it.platform == "echo360"]
        lecture_count = len(echo_items)

        is_echo_ok = echo_status == "authenticated" or (lecture_count > 0 and vtt_count > 0)
        status_val = "ok" if is_echo_ok else "warning"
        label_val = "LTI Bridge Active & Indexed" if is_echo_ok else "Awaiting Active Capture Tab"

        probes["echo360"] = {
            "status": status_val,
            "label": label_val,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "auth_status": echo_status,
                "lectures_indexed": lecture_count,
                "vtt_subtitles_cached": vtt_count,
                "lti_launch_target": lti_url or "Dynamic LTI 701",
            },
            "actions": [
                {"id": "open_echo", "label": "Open Echo360 ↗", "url": lti_url or "https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/701"}
            ]
        }
    except Exception as exc:
        probes["echo360"] = {"status": "error", "label": "Echo360 Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 4. Camoufox Anti-Detect Browser Engine
    t0 = time.perf_counter()
    try:
        from unisync.services.browser import get_camoufox_service
        camoufox_data = get_camoufox_service().get_status()
        installed = camoufox_data.get("installed", False)
        has_profile = camoufox_data.get("has_persistent_profile", False)
        mem_cookies = camoufox_data.get("cookies_in_memory", 0)

        if not installed:
            c_status = "offline"
            c_label = "Browser Binary Missing"
        elif has_profile and mem_cookies > 0:
            c_status = "ok"
            c_label = "Autonomous Stealth Profile Active"
        elif has_profile:
            c_status = "warning"
            c_label = "Profile Saved (Re-auth Advised)"
        else:
            c_status = "warning"
            c_label = "Initial Login Needed"

        probes["camoufox"] = {
            "status": c_status,
            "label": c_label,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "installed": installed,
                "has_persistent_profile": has_profile,
                "cookies_in_memory": mem_cookies,
                "profile_dir": camoufox_data.get("profile_dir", ""),
                "engine": "Firefox 135 Stealth Build",
            },
            "actions": [
                {"id": "camoufox_login", "label": "Login with Camoufox", "type": "api", "endpoint": "/api/v1/browser/login"},
                {"id": "camoufox_refresh", "label": "Silent Refresh", "type": "api", "endpoint": "/api/v1/browser/refresh"}
            ]
        }
    except Exception as exc:
        probes["camoufox"] = {"status": "error", "label": "Camoufox Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 5. Thin Bridge Userscript & Active Tabs
    t0 = time.perf_counter()
    try:
        active_tabs = [tab for tab in _bridge_tabs.values() if now - tab.get("last_seen", 0) < 90]
        last_ping = sync_metadata.get("last_bridge_ping", 0.0)
        ping_delta = round(now - last_ping, 1) if last_ping > 0 else None
        latest_ver = _get_latest_userscript_version()
        b_version = sync_metadata.get("bridge_version") or ""
        b_active = len(active_tabs) > 0 and (ping_delta is not None and ping_delta < 90)

        probes["bridge"] = {
            "status": "ok" if b_active else "idle",
            "label": f"{len(active_tabs)} Tab(s) Connected (Deprecated)" if b_active else "Userscript Idle (Deprecated)",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "active_tabs_count": len(active_tabs),
                "active_tabs": [{"hostname": t.get("hostname", ""), "title": t.get("title", ""), "seconds_ago": round(now - t.get("last_seen", now), 1)} for t in active_tabs[:5]],
                "last_heartbeat_seconds_ago": ping_delta,
                "script_version": f"v{b_version}" if b_version else "Not detected",
                "latest_version": f"v{latest_ver}",
                "pending_bridge_commands": len(_bridge_commands),
                "deprecated": True,
                "replacement": "Camoufox stealth browser engine",
            },
            "actions": [
                {"id": "download_userscript", "label": f"Download Userscript v{latest_ver} (Deprecated)", "url": "/userscript/echo360-connector.user.js"}
            ]
        }
    except Exception as exc:
        probes["bridge"] = {"status": "error", "label": "Bridge Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 6. EdStem Ingestion Platform
    t0 = time.perf_counter()
    try:
        from unisync.api.edstem import _load_settings, _LAST_EDSTEM_AUTO_SYNC_TS
        ed_settings = _load_settings()
        has_token = bool(ed_settings.get("edstem_api_token"))
        from unisync.core.path_service import get_path_service
        snaps_file = get_path_service().get_user_root() / "libraries" / "edstem_snapshots.json"
        snap_count = 0
        if snaps_file.exists():
            try:
                snaps_data = json.loads(snaps_file.read_text(encoding="utf-8"))
                snap_count = len(snaps_data) if isinstance(snaps_data, dict) else 0
            except Exception:
                pass
        
        last_ed_run = _LAST_EDSTEM_AUTO_SYNC_TS
        cooldown_rem = max(0.0, round(300.0 - (now - last_ed_run), 1)) if last_ed_run > 0 else 0.0

        probes["edstem"] = {
            "status": "ok" if has_token else "offline",
            "label": "API Connected & Ingested" if has_token else "API Token Not Configured",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "token_configured": has_token,
                "cached_snapshots": snap_count,
                "cooldown_remaining_seconds": cooldown_rem,
                "last_sync_timestamp": last_ed_run if last_ed_run > 0 else None,
            },
            "actions": [
                {"id": "edstem_sync", "label": "Sync EdStem Now", "type": "api", "endpoint": "/api/v1/edstem/sync"}
            ]
        }
    except Exception as exc:
        probes["edstem"] = {"status": "error", "label": "EdStem Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 7. WebSocket Real-Time Push Bus
    t0 = time.perf_counter()
    try:
        from unisync.api.sync_ws import get_sync_broadcaster, _USE_WS
        broadcaster = get_sync_broadcaster()
        client_count = len(broadcaster._connections.get("global", set()))
        probes["websocket"] = {
            "status": "ok" if client_count > 0 else "idle",
            "label": f"Push Bus Live ({client_count} client{'s' if client_count != 1 else ''})" if client_count > 0 else "Push Bus Standing By (0 clients)",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "active_connections": client_count,
                "channel_path": "/api/v1/sync/ws",
                "keepalive_interval": "30s ping",
                "broadcaster_enabled": _USE_WS,
            },
            "actions": []
        }
    except Exception as exc:
        probes["websocket"] = {"status": "error", "label": "WebSocket Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    # 8. Storage Engine & Media Semaphore
    t0 = time.perf_counter()
    try:
        import shutil
        from unisync.core.download_registry import DownloadRegistry
        reg = DownloadRegistry.get_instance()
        all_items = reg.list_all()
        total_assets = len(all_items)
        total_bytes = sum(getattr(it, "size_bytes", 0) for it in all_items)
        storage_mb = round(total_bytes / (1024 * 1024), 2)
        
        # Disk usage of user root
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
        free_gb = 0.0
        try:
            usage = shutil.disk_usage(user_root)
            free_gb = round(usage.free / (1024 ** 3), 1)
        except Exception:
            pass

        probes["storage"] = {
            "status": "ok",
            "label": f"{total_assets:,} Files ({storage_mb:,.1f} MB)",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": {
                "total_manifest_assets": total_assets,
                "total_storage_mb": storage_mb,
                "free_disk_space_gb": free_gb,
                "media_semaphore_busy": _download_sem.locked(),
                "atomic_writes_enabled": True,
            },
            "actions": [
                {"id": "open_inventory", "label": "View Inventory ↗", "type": "js", "fn": "openInventoryModal"}
            ]
        }
    except Exception as exc:
        probes["storage"] = {"status": "error", "label": "Storage Probe Failed", "latency_ms": 0, "details": {"error": str(exc)}, "actions": []}

    return probes


@router.get("/progress")
async def get_progress() -> dict[str, Any]:
    """Returns the current progress of the v3 Native Sync."""
    return sync_status


@router.get("/status")
async def get_sync_status_endpoint() -> dict[str, Any]:
    """Returns comprehensive sync state, bridge health, and lecture timestamps."""
    return get_computed_sync_status()


@router.get("/probes")
async def get_sync_probes_endpoint() -> dict[str, Any]:
    """Runs comprehensive diagnostic health probes across all 8 UniSync subsystems."""
    return get_all_probes()


@router.get("/courses")
async def get_enrolled_courses() -> list[dict[str, Any]]:
    """Return enrolled courses dynamically discovered from libraries.json."""
    import re
    from unisync.api.lectures import _load_libraries
    libs = _load_libraries()
    courses = []
    for cid, lib in libs.items():
        name = lib.get("name", "")
        m = re.search(r"([A-Z]{4}\d{5})", name)
        code = m.group(1) if m else ""
        clean_name = re.sub(r"\([A-Z]{4}\d{5}[^\)]*\)", "", name).strip() or name
        canvas_cid = str(lib.get("canvas_course_id") or cid)
        lec_url = lib.get("lecture_capture_url") or (
            f"https://canvas.lms.unimelb.edu.au/courses/{canvas_cid}/external_tools/701"
            if canvas_cid.isdigit()
            else ""
        )
        # Prefer local backend thumbnail endpoint for reliable, non-expiring Canvas banners
        thumb_url = f"/api/v1/sync/courses/{code or canvas_cid}/thumbnail" if (code or canvas_cid) else lib.get("image_download_url", "")
        courses.append({
            "code": code or "COMMUNITY",
            "name": clean_name,
            "full_name": name,
            "canvas_course_id": canvas_cid,
            "is_academic": bool(code),
            "lecture_capture_url": lec_url,
            "course_url": f"https://canvas.lms.unimelb.edu.au/courses/{canvas_cid}",
            "echo360_course_id": lib.get("echo360_course_id", ""),
            "image_download_url": thumb_url,
        })
    courses.sort(key=lambda c: (not c["is_academic"], not bool(c["echo360_course_id"]), c["code"]))
    return courses


@router.get("/courses/{code_or_id}/thumbnail")
async def get_course_thumbnail(code_or_id: str):
    """Serve local Canvas subject thumbnail banner with fallback."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from unisync.core.path_service import get_path_service

    data_dir = get_path_service().get_user_root()
    courses_dir = data_dir / "courses"
    clean_target = code_or_id.split("_")[0].upper()

    banner_map = {
        "COMP10001": courses_dir / "COMP10001_2026_SM2" / "raw" / "canvas" / "241246" / "banner-comp10001.png",
        "241246": courses_dir / "COMP10001_2026_SM2" / "raw" / "canvas" / "241246" / "banner-comp10001.png",
        "BLAW10001": courses_dir / "handbooks" / "raw" / "canvas" / "240718" / "banner-blaw10001-1.png",
        "240718": courses_dir / "handbooks" / "raw" / "canvas" / "240718" / "banner-blaw10001-1.png",
        "CCDP10003": courses_dir / "CCDP10003_2026_SM2" / "raw" / "canvas" / "239834" / "Banner_Subject.jpg",
        "239834": courses_dir / "CCDP10003_2026_SM2" / "raw" / "canvas" / "239834" / "Banner_Subject.jpg",
        "INFO10003": courses_dir / "INFO10003_2026_SM2" / "raw" / "canvas" / "237296" / "pexels-jakubzerdzicki-18686859a.jpg",
        "237296": courses_dir / "INFO10003_2026_SM2" / "raw" / "canvas" / "237296" / "pexels-jakubzerdzicki-18686859a.jpg",
    }

    if clean_target in banner_map and banner_map[clean_target].exists():
        p = banner_map[clean_target]
        media_type = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(p, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    # Dynamic search fallback in courses directory
    if courses_dir.exists():
        for c_folder in courses_dir.glob("*"):
            if not c_folder.is_dir():
                continue
            if clean_target in c_folder.name.upper() or code_or_id in c_folder.name:
                for cand in c_folder.glob("raw/canvas/**/*"):
                    if cand.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and (
                        "banner" in cand.name.lower() or "card" in cand.name.lower() or "subject" in cand.name.lower()
                    ):
                        media_type = "image/png" if cand.suffix.lower() == ".png" else "image/jpeg"
                        return FileResponse(cand, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    raise HTTPException(status_code=404, detail=f"Thumbnail not found for course {code_or_id}")



@router.post("/bridge/heartbeat")
async def bridge_heartbeat(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Heartbeat from the browser userscript reporting tab presence and auth status."""
    sync_metadata["last_bridge_ping"] = time.time()
    if str(body.get("message", "")).startswith("Echo360 import:"):
        logger.info("%s", body["message"])
    tab = body.get("tab") or body.get("hostname") or ""
    tab_id = str(body.get("tab_id") or tab)
    _bridge_tabs[tab_id] = {"tab_id": tab_id, "tab": tab, "title": str(body.get("title") or ""), "hostname": body.get("hostname", ""), "platform": body.get("platform", "generic"), "auth_status": body.get("auth_status", ""), "last_seen": time.time()}
    if tab:
        sync_metadata["bridge_tab"] = tab
    if "version" in body:
        sync_metadata["bridge_version"] = str(body["version"])

    # P2-19: platforms now report their own auth status so Canvas and Echo360
    # sessions are tracked independently. Body may carry a `platform` field
    # ("canvas" | "echo360" | "outlook" | "perusall" | "generic"). When present
    # we store auth only under that platform's key; the global `auth_status` is
    # kept for backward compat but is derived from the uni platforms.
    auth = body.get("auth_status", "")
    message = body.get("message", "")
    platform = str(body.get("platform", "") or "").lower()

    if platform == "canvas":
        sync_metadata["canvas_auth_status"] = auth
        sync_metadata["canvas_auth_message"] = message
    elif platform == "echo360":
        sync_metadata["echo360_auth_status"] = auth
        sync_metadata["echo360_auth_message"] = message
    elif platform not in ("", "generic", "outlook", "perusall"):
        # Unknown platform — treat as generic (do not clobber uni auth states)
        sync_metadata["auth_status"] = auth
        sync_metadata["auth_message"] = message
    elif auth:
        # Legacy / no platform: keep global in sync but never mark uni states
        # as authenticated just because some non-uni page is active.
        sync_metadata["auth_status"] = auth
        sync_metadata["auth_message"] = message

    # Derive the global auth_status from the uni platforms (Canvas most important).
    global_auth = "unknown"
    if sync_metadata.get("canvas_auth_status") == "waiting_for_auth" or (
        sync_metadata.get("canvas_auth_status") != "authenticated"
        and sync_metadata.get("echo360_auth_status") == "waiting_for_auth"
    ):
        global_auth = "waiting_for_auth"
    elif sync_metadata.get("canvas_auth_status") == "authenticated" or sync_metadata.get("echo360_auth_status") == "authenticated":
        global_auth = "authenticated"
    if global_auth != "unknown":
        sync_metadata["auth_status"] = global_auth

    _schedule_broadcast()
    return {"status": "ok", "is_syncing": sync_status.get("is_syncing", False)}


@router.get("/credentials/status")
async def get_credentials_status() -> dict[str, Any]:
    """Returns the readiness status of the backend cookie jar.

    Used by the Library v3 panel to show Ready / Install Bridge state
    without leaking raw cookie names. Keeps the debug GET /credentials
    endpoint separate.
    """
    canvas_cookies = get_cookies_for_domain("canvas.lms.unimelb.edu.au")
    echo_cookies = get_cookies_for_domain("echo360.net.au")
    return {
        "has_canvas_cookies": bool(canvas_cookies),
        "has_echo360_cookies": bool(echo_cookies),
        "total_cookies": len(global_cookie_jar),
    }

@router.post("/credentials")
async def sync_credentials(background_tasks: BackgroundTasks, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Receives cookies from the Thin Bridge userscript."""
    cookies = body.get("cookies", [])
    # P0-3: protect global_cookie_jar mutations from concurrent async access
    async with _sync_lock:
        for c in cookies:
            domain = c.get("domain", "")
            name = c.get("name", "")
            value = c.get("value", "")
            if domain and name:
                global_cookie_jar[(domain, name)] = value
            
    logger.info(f"Received {len(cookies)} cookies from sync bridge.")
    
    # Check under lock to avoid TOCTOU race on is_syncing flag
    async with _sync_lock:
        should_start = not sync_status.get("is_syncing")
        if should_start:
            canvas_cookies = get_cookies_for_domain("canvas.lms.unimelb.edu.au")
            echo_cookies = get_cookies_for_domain("echo360.net.au")
            if canvas_cookies and echo_cookies:
                background_tasks.add_task(_run_sync_pipeline, canvas_cookies, echo_cookies)
                logger.info("Auto-started native sync pipeline based on received cookies.")
            
    return {"status": "ok", "count": len(global_cookie_jar)}

@router.get("/credentials")
async def get_credentials() -> dict[str, Any]:
    """Debug endpoint to see what cookies are in the jar."""
    return {"keys": list(global_cookie_jar.keys())}


@router.get("/canvas/synced-files")
async def get_canvas_synced_files(course_id: str = "") -> dict[str, Any]:
    """Returns the set of Canvas file IDs that have already been ingested into a Knowledge Base.
    
    This allows the hybrid userscript scraper to skip downloading redundant files
    without needing to fetch full manifests or re-download large binaries.
    """
    data = _load_canvas_files()
    cid = str(course_id).strip()
    if cid:
        c_entry = data.get(cid, {})
        files_dict = c_entry.get("files", {})
        return {"synced_file_ids": list(files_dict.keys())}
    
    # Return all registered Canvas file IDs across all courses
    all_ids: list[str] = []
    for c_entry in data.values():
        if isinstance(c_entry, dict) and "files" in c_entry:
            all_ids.extend(list(c_entry["files"].keys()))
    return {"synced_file_ids": all_ids}


@router.post("/canvas/upload")
async def upload_canvas_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    course_id: str = Form(...),
    canvas_file_id: str = Form(...),
    course_name: str = Form(""),
    folder: str = Form(""),
    filename_hint: str = Form(""),
    kb_name: str = Form(""),
) -> dict[str, Any]:
    """Receives a Canvas PDF/document blob downloaded in the user's authenticated browser session.
    
    Why: Canvas course files (especially inside institutional LMS instances like UniMelb Canvas)
    require active SSO session cookies to download and cannot be fetched unauthenticated from
    the backend without API tokens. The userscript streams the binary blob directly here.
    
    Workflow:
      1. Resolves target Knowledge Base from `libraries.json` or falls back to course ID.
      2. Ensures the destination directory exists in `raw/canvas/{course_id}/`.
      3. Safely sanitizes filename and writes binary stream directly to disk.
      4. Registers the file record in `canvas_files.json` for delta sync deduplication.
      5. Enqueues background RAG initialization/indexing so the document is searchable.
    """
    from unisync.api.lectures import _load_libraries, _ensure_kb_exists
    from unisync.core.storage import current_kb_manager
    from unisync.core.validator import DocumentValidator
    from datetime import datetime, timezone
    from uuid import uuid4

    cid = str(course_id).strip()
    fid = str(canvas_file_id).strip()
    
    # 1. Resolve Target Knowledge Base Name
    target_kb = kb_name.strip()
    if not target_kb and cid:
        libs = _load_libraries()
        if cid in libs and libs[cid].get("kb_name"):
            target_kb = libs[cid]["kb_name"]
        else:
            for lib in libs.values():
                if lib.get("canvas_course_id") == cid and lib.get("kb_name"):
                    target_kb = lib["kb_name"]
                    break
    
    if not target_kb:
        target_kb = f"canvas_course_{cid}"
    
    # Ensure KB directory and registration exists
    _ensure_kb_exists(target_kb, description=f"Knowledge base for Canvas course {course_name or cid}")
    
    manager = current_kb_manager()
    kb_path = manager.get_knowledge_base_path(target_kb)
    raw_dir = kb_path / "raw"
    
    # 2. Sanitize and determine destination path
    original_filename = file.filename or filename_hint or f"file_{fid}.pdf"
    try:
        sanitized_filename = DocumentValidator.validate_upload_safety(original_filename, None)
    except Exception:
        sanitized_filename = f"canvas_file_{fid}.pdf"
    
    # Place Canvas files under raw/canvas/{course_id}/ (or subfolder) to avoid collisions
    dest_dir = raw_dir / "canvas" / cid
    if folder:
        clean_folder = folder.replace("\\", "/").strip("/")
        if clean_folder and clean_folder != "canvas":
            dest_dir = dest_dir / clean_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    dest_path = dest_dir / sanitized_filename
    
    # 3. Stream bytes to disk
    content = await file.read()
    dest_path.write_bytes(content)
    file_size = len(content)
    
    rel_kb_path = str(dest_path.relative_to(kb_path))
    
    # 4. Record into canvas_files.json under lock
    async with _sync_lock:
        c_files_data = _load_canvas_files()
        c_entry = c_files_data.setdefault(cid, {
            "kb_name": target_kb,
            "course_name": course_name or f"Course {cid}",
            "files": {}
        })
        c_entry["kb_name"] = target_kb
        if course_name:
            c_entry["course_name"] = course_name
            
        task_id = f"canvas_files_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        c_entry["files"][fid] = {
            "display_name": original_filename,
            "size": file_size,
            "content_type": file.content_type or "application/pdf",
            "kb_path": rel_kb_path,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
        }
        _save_canvas_files(c_files_data)

        # Keep the user-facing provenance inventory in step with the Canvas
        # file ledger. Previously uploads were deduplicated in
        # canvas_files.json but never appeared in the dashboard inventory.
        from unisync.core.download_registry import get_registry
        get_registry().register(
            platform="canvas",
            item_id=fid,
            title=original_filename,
            direct_link=(
                f"https://canvas.lms.unimelb.edu.au/courses/{cid}/files/{fid}/"
                "download?download_frd=1"
            ),
            local_filepath=dest_path,
            course_id=cid,
            course_name=course_name or f"Course {cid}",
            size_bytes=file_size,
            content_type=file.content_type or "application/pdf",
            metadata={"folder": folder, "kb_name": target_kb, "task_id": task_id},
        )
        
        # Record in delta diff state for UI change feed
        state = _load_sync_state()
        _record_diff(state, course_name or f"Course {cid}", "NEW_FILE", f"+ FILE: {original_filename} ({file_size} bytes)")
        _save_sync_state(state)
    
    log_sync(f"[{course_name or cid}] Ingested Canvas file: {original_filename} ({file_size} bytes)", 50, 100)
    logger.info(f"Saved Canvas file {fid} ({original_filename}) to KB '{target_kb}' at {dest_path}")
    
    sync_metadata["last_bridge_ping"] = time.time()
    sync_metadata["auth_status"] = "authenticated"

    # 5. Enqueue background KB processing / incremental indexing task
    try:
        from unisync.core.storage import run_upload_processing_task, _mark_kb_queued_for_processing
        _mark_kb_queued_for_processing(
            manager,
            target_kb,
            task_id,
            f"Processing uploaded Canvas file: {sanitized_filename}",
        )
        background_tasks.add_task(
            run_upload_processing_task,
            kb_name=target_kb,
            base_dir=str(manager.base_dir),
            uploaded_file_paths=[str(dest_path)],
            task_id=task_id,
            rag_provider="llamaindex",
        )
    except Exception as exc:
        logger.warning(f"Could not enqueue background indexing task for KB '{target_kb}': {exc}")
        
    return {
        "status": "ok",
        "canvas_file_id": fid,
        "filename": sanitized_filename,
        "size": file_size,
        "kb_name": target_kb,
        "path": rel_kb_path,
    }


def _upsert_canvas_assignment(course_id: str, course_name: str, assignment: dict[str, Any]) -> None:
    """Persist one Canvas assignment without discarding an existing full rubric."""
    assignment_id = str(assignment.get("id") or "")
    if not assignment_id:
        return

    title = assignment.get("name") or "Assignment"
    due_at = assignment.get("due_at") or ""
    html_url = assignment.get("html_url") or (
        f"https://canvas.lms.unimelb.edu.au/courses/{course_id}/assignments/{assignment_id}"
    )
    submission = assignment.get("submission") or {}
    submission_state = submission.get("workflow_state") or "unsubmitted"
    rubric = assignment.get("rubric")

    payload = {
        "title": title,
        "due_at": due_at or None,
        "course": course_name,
        "kind": "assignment",
        "submission_status": submission_state,
        "is_graded": submission_state == "graded" or submission.get("score") is not None,
        "score": submission.get("score"),
        "points_possible": assignment.get("points_possible"),
        "grade": str(submission["grade"]) if submission.get("grade") is not None else None,
        "grading_type": assignment.get("grading_type"),
        "submitted_at": submission.get("submitted_at"),
        "graded_at": submission.get("graded_at"),
        "description": assignment.get("description") or "",
        "submission_types": assignment.get("submission_types"),
        "allowed_extensions": assignment.get("allowed_extensions"),
        "submission_comments": submission.get("submission_comments"),
        "url": html_url,
        "source": "canvas",
        "source_id": f"canvas:{course_id}:{assignment_id}",
    }
    # An absent field is a partial response. An explicit list, including [], is
    # authoritative and can safely update the stored assignment.
    if isinstance(rubric, list):
        payload["rubric"] = rubric
    if submission_state in ("submitted", "graded"):
        payload["status"] = "done"

    try:
        from unisync.services.todos import get_todo_service

        todo_service = get_todo_service()
        existing = [
            item
            for item in todo_service.list_items()
            if getattr(item, "source_id", None) == payload["source_id"]
            or (getattr(item, "url", None) and getattr(item, "url", None) == html_url)
            or (html_url in (getattr(item, "notes", None) or ""))
            or (item.title == title and item.course == course_name)
        ]
        if existing:
            todo_service.update_item(existing[0].id, payload)
        else:
            payload["priority"] = "high" if "quiz" in title.lower() or "exam" in title.lower() else "normal"
            payload["notes"] = f"Canvas Assignment: {html_url}"
            todo_service.create_item(payload)
    except Exception as exc:
        logger.debug("Failed to upsert Canvas assignment %s (%s)", assignment_id, type(exc).__name__)


@router.post("/payload")
async def sync_payload(background_tasks: BackgroundTasks, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Receives JSON payloads directly from the browser to bypass strict HttpOnly cookie blocks."""
    payload_type = body.get("data", {}).get("type")
    
    sync_metadata["last_bridge_ping"] = time.time()
    sync_metadata["auth_status"] = "authenticated"
    if body.get("hostname"):
        sync_metadata["bridge_tab"] = body["hostname"]

    # P0-3: protect sync_status mutations with async lock to prevent races
    # with concurrent sync_credentials or _run_sync_pipeline handlers.
    async with _sync_lock:
        sync_status["is_syncing"] = True
        sync_status["log"].clear()
    
    if payload_type == "canvas_courses":
        courses = body["data"]["courses"]
        state = _load_sync_state()
        new_items_count = 0
        due_shifts_count = 0
        
        log_sync(f"Discovered {len(courses)} Canvas courses via Hybrid browser scrape!", 10, 100)
        
        for idx, course in enumerate(courses):
            c_id = str(course.get("id") or idx)
            c_name = course.get("name", "Unnamed")
            modules = course.get("modules", [])
            
            c_state = state.setdefault("courses", {}).setdefault(c_id, {
                "name": c_name,
                "items": {},
            })
            c_state["name"] = c_name
            
            if modules:
                log_sync(f"[{c_name}] Scanning {len(modules)} modules for changes…", 20 + int(60 * (idx / max(1, len(courses)))), 100)
                page_count = 0
                for m in modules:
                    m_name = m.get("name") or "Module"
                    for item in m.get("items", []):
                        i_id = str(item.get("id") or item.get("title") or "")
                        i_title = item.get("title") or "Item"
                        i_type = item.get("type") or "Page"
                        i_due = item.get("due_at") or ""
                        
                        prev_item = c_state["items"].get(i_id)
                        if not prev_item:
                            # New item detected
                            new_items_count += 1
                            if i_type == "Page" and item.get("body"):
                                page_count += 1
                            due_suffix = f" (due {i_due[:10]})" if i_due else ""
                            _record_diff(state, c_name, "NEW_ITEM", f"+ NEW: [{m_name}] {i_title} ({i_type}){due_suffix}")
                        elif i_due and prev_item.get("due_at") and prev_item["due_at"] != i_due:
                            # Shifted due date detected
                            due_shifts_count += 1
                            _record_diff(state, c_name, "DUE_SHIFT", f"~ DUE: {i_title} shifted from {prev_item['due_at'][:10]} -> {i_due[:10]}")
                        
                        # Update snapshot
                        c_state["items"][i_id] = {
                            "title": i_title,
                            "type": i_type,
                            "due_at": i_due,
                            "module": m_name,
                        }

                        # Pragmatic Canvas todo registration
                        if i_due:
                            try:
                                from unisync.services.todos import get_todo_service
                                todo_svc = get_todo_service()
                                source_id = f"canvas:{c_id}:{i_id}"
                                existing_todos = [t for t in todo_svc.list_items() if getattr(t, "source_id", None) == source_id or (t.title == i_title and t.course == c_name)]
                                if not existing_todos:
                                    kind = "assignment" if "assign" in i_type.lower() else ("exam" if "quiz" in i_type.lower() else "admin")
                                    priority = "high" if "quiz" in i_type.lower() or "exam" in i_type.lower() else "normal"
                                    todo_svc.create_item({
                                        "title": i_title,
                                        "due_at": i_due,
                                        "kind": kind,
                                        "course": c_name,
                                        "priority": priority,
                                        "notes": f"Canvas {i_type}: {m_name}",
                                        "source": "canvas",
                                        "source_id": source_id,
                                    })
                                elif prev_item and prev_item.get("due_at") != i_due:
                                    todo_svc.update_item(existing_todos[0].id, {"due_at": i_due})
                            except Exception as todo_err:
                                logger.debug("Failed to upsert Canvas todo: %s", todo_err)
                        
                if page_count > 0:
                    log_sync(f"[{c_name}] Indexed {page_count} rich text pages!", 30 + int(60 * (idx / max(1, len(courses)))), 100)

            # Ingest Canvas Assignments with rich submissions, grades, rubrics, and descriptions
            assignments = course.get("assignments", [])
            if assignments:
                for assignment in assignments:
                    _upsert_canvas_assignment(c_id, c_name, assignment)
                    
        _save_sync_state(state)

        # Update course libraries with dynamic lecture capture URLs discovered from Canvas tabs
        try:
            from unisync.api.lectures import _load_libraries, _save_libraries
            libs = _load_libraries()
            updated_libs = False
            for course in courses:
                cid = str(course.get("id") or "")
                lec_url = course.get("lecture_capture_url")
                if cid and lec_url and cid in libs:
                    if libs[cid].get("lecture_capture_url") != lec_url:
                        libs[cid]["lecture_capture_url"] = lec_url
                        updated_libs = True
            if updated_libs:
                _save_libraries(libs)
                logger.info("Updated course libraries with dynamic Canvas Lecture Capture URLs")
        except Exception as lib_err:
            logger.debug("Failed saving lecture capture URLs into libraries: %s", lib_err)

        # Ingest and persist any newly harvested Echo360 embedded links across Canvas pages
        try:
            import re
            from unisync.core.path_service import get_path_service
            from unisync.core.file_io import atomic_write_json
            embeds_file = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
            embeds_dict: dict[str, list[dict[str, Any]]] = {}
            if embeds_file.exists():
                try:
                    embeds_dict = json.loads(embeds_file.read_text(encoding="utf-8"))
                except Exception:
                    embeds_dict = {}

            new_embeds_count = 0
            for course in courses:
                cid = str(course.get("id") or "")
                raw_embeds = course.get("echo_embeds", [])
                if not raw_embeds or not cid:
                    continue
                existing_items = embeds_dict.setdefault(cid, [])
                existing_keys = {
                    (it.get("link_id"),) if it.get("link_id")
                    else (it.get("uuid") or it.get("media_id"), None)
                    for it in existing_items
                }
                for em_item in raw_embeds:
                    # Canvas-wrapped URLs arrive percent-encoded — decode first.
                    if isinstance(em_item, dict):
                        em_raw_str = em_item.get("url") or ""
                        item_title = str(em_item.get("title") or em_item.get("page_title") or "").strip()
                        item_source_title = str(em_item.get("page_title") or em_item.get("title") or "").strip()
                    else:
                        em_raw_str = str(em_item)
                        item_title = ""
                        item_source_title = ""

                    try:
                        from urllib.parse import unquote, quote
                        em_scan = unquote(em_raw_str)
                    except Exception:
                        em_scan = em_raw_str
                    m = re.search(r"(?:media|lti|ui/player)/([a-f0-9\\-]+)", em_scan, re.I)
                    if m:
                        u_val = m.group(1)
                        m_link = re.search(r"linkId=([a-f0-9\\-]+)", em_scan, re.I)
                        link_val = m_link.group(1) if m_link else None
                        
                        # In Canvas LTI embeds, u_val is often the generic external tool placement ID,
                        # while link_id identifies the specific lecture or module recording.
                        if link_val:
                            existing_match = next((item for item in existing_items if item.get("link_id") == link_val), None)
                            uniq_key = (link_val,)
                        else:
                            existing_match = next((item for item in existing_items if (item.get("uuid") or item.get("media_id")) == u_val and not item.get("link_id")), None)
                            if not existing_match:
                                existing_match = next((item for item in existing_items if f"/{u_val}/" in (item.get("media_url") or "")), None)
                            uniq_key = (u_val, None)

                        from unisync.api.lectures import _KNOWN_STANDALONE_TITLES
                        if u_val in _KNOWN_STANDALONE_TITLES:
                            item_title = item_title or _KNOWN_STANDALONE_TITLES[u_val]
                            item_source_title = item_source_title or _KNOWN_STANDALONE_TITLES[u_val]

                        full_url = em_scan if em_scan.startswith("http") else f"https://echo360.net.au/{em_scan.lstrip('/')}"
                        if link_val:
                            canonical_launch = f"https://canvas.lms.unimelb.edu.au/courses/{cid}/external_tools/retrieve?display=borderless&url={quote(full_url, safe='')}"
                        elif "ui/player" in em_scan:
                            canonical_launch = full_url
                        else:
                            canonical_launch = f"https://echo360.net.au/media/{u_val}/public"

                        # Update existing match with richer title / launch url if previously placeholder
                        if existing_match:
                            if item_title and existing_match.get("title") in ("embedded content", "Echo360 Video", "", None):
                                existing_match["title"] = item_title
                            if item_source_title and existing_match.get("source_title") in ("Canvas Page Video", "", None):
                                existing_match["source_title"] = item_source_title
                            if link_val and (not existing_match.get("canvas_launch_url") or "public" in existing_match.get("canvas_launch_url", "")):
                                existing_match["canvas_launch_url"] = canonical_launch

                        # A previous discovery may only have a public-style URL.
                        # Replace it when Canvas later supplies the secure player.
                        if existing_match and "ui/player" in em_scan and "secureLinkAccessDataId=" in em_scan:
                            m_sec = re.search(r"secureLinkAccessDataId=([a-f0-9\\-]+)", em_scan, re.I)
                            if m_sec:
                                existing_match.update({
                                    "player_url": em_scan,
                                    "provider_launch_url": em_scan,
                                    "canvas_launch_url": em_scan,
                                    "secure_link_access_data_id": m_sec.group(1),
                                })
                                new_embeds_count += 1
                        elif not existing_match and uniq_key not in existing_keys:
                            existing_keys.add(uniq_key)
                            # ui/player embeds carry the real media id + secure
                            # link in the query string — preserve them.
                            m_mid = re.search(r"currentMediaId=([a-f0-9\\-]+)", em_scan, re.I)
                            media_val = m_mid.group(1) if m_mid else u_val
                            m_sec = re.search(r"secureLinkAccessDataId=([a-f0-9\\-]+)", em_scan, re.I)
                            
                            # Resolve clean title from known standalone lookup or dynamic player fetch
                            resolved_title = item_title
                            if not resolved_title or resolved_title in ("Echo360 Video", "embedded content"):
                                if media_val in _KNOWN_STANDALONE_TITLES:
                                    resolved_title = _KNOWN_STANDALONE_TITLES[media_val]
                                elif u_val in _KNOWN_STANDALONE_TITLES:
                                    resolved_title = _KNOWN_STANDALONE_TITLES[u_val]
                                else:
                                    from unisync.api.lectures import _fetch_title_from_player_url
                                    resolved_title = _fetch_title_from_player_url(full_url) or resolved_title

                            clean_final_title = resolved_title or "Echo360 Video"
                            clean_final_src = item_source_title if item_source_title and item_source_title != "Canvas Page Video" else clean_final_title

                            existing_items.append({
                                "provider": "echo360",
                                "title": clean_final_title,
                                "source_title": clean_final_src,
                                "source_kind": "page",
                                "provider_launch_url": full_url,
                                "canvas_launch_url": canonical_launch,
                                "player_url": full_url,
                                "uuid": u_val,
                                "media_id": media_val,
                                "link_id": link_val,
                                "secure_link_access_data_id": m_sec.group(1) if m_sec else None,
                                "resolved_at": datetime.now(timezone.utc).isoformat(),
                            })
                            new_embeds_count += 1
            if new_embeds_count > 0:
                embeds_file.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(embeds_file, embeds_dict)
                logger.info("Persisted %d new Echo360 embed target(s) to echo360_embeds.json", new_embeds_count)
        except Exception as embed_save_err:
            logger.debug("Failed saving newly discovered echo embeds: %s", embed_save_err)

        # Background auto-sync of EdStem lessons/submissions if configured
        try:
            from unisync.api.edstem import sync_all_configured_edstem_courses
            asyncio.create_task(asyncio.to_thread(sync_all_configured_edstem_courses))
        except Exception as ed_err:
            logger.debug("Background EdStem sync trigger failed: %s", ed_err)

        # Auto-trigger on-disk scrubber in background after Canvas sync
        try:
            from unisync.services.disk_cleaner import clean_disk
            background_tasks.add_task(clean_disk)
        except Exception as _scrub_err:
            logger.debug("Failed to schedule background disk scrubber after Canvas sync: %s", _scrub_err)

        if new_items_count == 0 and due_shifts_count == 0:
            log_sync(f"Hybrid Canvas Sync Complete! All {len(courses)} courses up to date. (0 new items, zero compute spent)", 100, 100)
        else:
            log_sync(f"Hybrid Canvas Sync Complete! Synced {new_items_count} new item(s) & {due_shifts_count} due date update(s).", 100, 100)
        
    elif payload_type == "echo360_sections":
        from uuid import uuid4
        from unisync.api.lectures import (
            _load_libraries,
            _ensure_kb_exists,
            _existing_echo360_ids,
            _cleanup_failed_echo360_id,
            _root,
            _read,
            _write,
            _safe_media_title,
            _download_and_process_echo360,
            _process,
            Echo360Source,
        )
        
        sections = body["data"]["sections"]
        log_sync(f"Discovered {len(sections)} Echo360 sections via Hybrid browser scrape!", 10, 100)
        
        # Cache syllabus to data/cache/echo360_syllabus.json for lecture schedule tracking (merge, don't overwrite)
        try:
            from unisync.core.path_service import get_path_service
            from unisync.core.file_io import atomic_write_json as _atomic_write_json
            syl_cache = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
            syl_cache.parent.mkdir(parents=True, exist_ok=True)
            existing_sections = []
            if syl_cache.exists():
                try:
                    existing_sections = json.loads(syl_cache.read_text(encoding="utf-8")) or []
                except Exception:
                    existing_sections = []
            
            sec_dict = {s.get("sectionId"): s for s in existing_sections if s.get("sectionId")}
            for new_sec in sections:
                sid = new_sec.get("sectionId")
                if not sid:
                    continue
                if sid not in sec_dict:
                    sec_dict[sid] = new_sec
                else:
                    existing_lessons = {
                        (item.get("lesson", {}).get("lesson", item.get("lesson", {})).get("id")): item
                        for item in sec_dict[sid].get("syllabus", [])
                        if (item.get("lesson", {}).get("lesson", item.get("lesson", {})).get("id"))
                    }
                    for new_item in new_sec.get("syllabus", []):
                        lid = new_item.get("lesson", {}).get("lesson", new_item.get("lesson", {})).get("id")
                        if lid:
                            existing_lessons[lid] = new_item
                    sec_dict[sid]["syllabus"] = list(existing_lessons.values())
                    if new_sec.get("courseName"):
                        sec_dict[sid]["courseName"] = new_sec["courseName"]
                    if new_sec.get("name"):
                        sec_dict[sid]["name"] = new_sec["name"]

            _atomic_write_json(syl_cache, list(sec_dict.values()))
        except Exception as _syl_err:
            logger.debug("Failed caching echo360_syllabus.json: %s", _syl_err)
            
        libs = _load_libraries()
        # P1-10: cache disk scan — avoids O(n) on every 15s poll
        existing_ids = _cached_existing_ids(include_failed=False)
        state = _load_sync_state()
        total_queued = 0
        total_subs = 0
        
        for idx, sec in enumerate(sections):
          try:
            sec_id = sec.get("sectionId")
            syllabus = sec.get("syllabus", [])
            if sec_id and syllabus:
                kb_name = ""
                course_name = sec.get("courseName") or sec.get("sectionName") or sec.get("name") or ""
                for lib in libs.values():
                    if lib.get("echo360_course_id") == sec_id:
                        kb_name = lib.get("kb_name", "")
                        course_name = lib.get("name", "") or course_name
                        break
                if kb_name:
                    _ensure_kb_exists(kb_name)
                
                sec_state = state.setdefault("courses", {}).setdefault(sec_id, {
                    "name": course_name,
                    "items": {},
                })
                sec_state["name"] = course_name
                    
                log_sync(f"  - Echo360 Section {sec_id} has {len(syllabus)} lectures.", 20 + int(60 * (idx / max(1, len(sections)))), 100)
                sub_count = 0
                sec_queued = 0

                # P2-12: prioritize recent lectures — students get recent material first
                syllabus_sorted = sorted(syllabus, key=_lecture_date, reverse=True)

                for item in syllabus_sorted:
                    les_obj = item.get("lesson") or {}
                    les_inner = les_obj.get("lesson") or les_obj
                    lesson_id = les_inner.get("id") or item.get("id")
                    
                    if not lesson_id:
                        continue
                        
                    safe_id = lesson_id.replace(":", "_")
                    title = les_inner.get("name") or les_obj.get("name") or "Echo360 Lecture"
                    lecture_date = (les_inner.get("timing") or {}).get("start", "")
                    
                    # 1. Cache subtitles
                    if item.get("subtitles") or item.get("subtitles_json"):
                        from unisync.core.path_service import get_path_service
                        cache_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        
                        vtt_text = item.get("subtitles")
                        if not vtt_text and item.get("subtitles_json"):
                            s_json = item["subtitles_json"]
                            cues = []
                            if isinstance(s_json, dict):
                                cues = s_json.get("data", {}).get("contentJSON", {}).get("cues", []) or s_json.get("cues", [])
                            elif isinstance(s_json, list):
                                cues = s_json
                            if cues:
                                vtt_text = _cues_to_vtt(cues)
                            else:
                                (cache_dir / f"{safe_id}.json").write_text(json.dumps(s_json), encoding="utf-8")

                        if vtt_text:
                            vtt_path = cache_dir / f"{safe_id}.vtt"
                            vtt_path.write_text(vtt_text, encoding="utf-8")

                            # Subtitle tracks are first-class local assets too.
                            # Record them in the shared provenance inventory so
                            # the dashboard reflects cached Echo360 material.
                            from unisync.core.download_registry import get_registry
                            get_registry().register(
                                platform="echo360",
                                item_id=f"{lesson_id}:subtitles",
                                title=f"{title} — subtitles.vtt",
                                direct_link=(
                                    f"https://echo360.net.au/media/download/{lesson_id}/"
                                    f"transcript.vtt?lessonId={lesson_id}"
                                ),
                                local_filepath=vtt_path,
                                course_id=sec_id,
                                course_name=course_name,
                                size_bytes=vtt_path.stat().st_size,
                                content_type="text/vtt",
                                metadata={
                                    "asset_type": "subtitle",
                                    "lecture_date": lecture_date,
                                    "lesson_id": lesson_id,
                                    "source_url": f"https://echo360.net.au/section/{sec_id}/home",
                                },
                            )
                            
                        sub_count += 1
                        total_subs += 1

                    # 1b. Cache thumbnail poster if captured
                    if item.get("thumbnail_b64"):
                        from unisync.core.path_service import get_path_service
                        import base64
                        thumb_cache_dir = get_path_service().get_user_root() / "cache" / "echo360_thumbnails"
                        thumb_cache_dir.mkdir(parents=True, exist_ok=True)
                        b64_raw = item["thumbnail_b64"]
                        if "," in b64_raw:
                            b64_raw = b64_raw.split(",", 1)[1]
                        try:
                            t_bytes = base64.b64decode(b64_raw)
                            if len(t_bytes) > 50:
                                t_path = thumb_cache_dir / f"{safe_id}.jpg"
                                t_path.write_bytes(t_bytes)
                                if media_id:
                                    safe_mid = media_id.replace(":", "_")
                                    (thumb_cache_dir / f"{safe_mid}.jpg").write_bytes(t_bytes)
                        except Exception as _thumb_err:
                            logger.debug("Failed saving thumbnail poster for %s: %s", lesson_id, _thumb_err)
                        
                    # 2. Queue lecture import
                    media_url = item.get("media_url") or ""
                    media_kind = item.get("media_kind") or "mp4"

                    # Embed resolution arrives through the Echo360 sections
                    # payload. Merge resolved stream metadata back into the
                    # canonical embed catalog so the catalog can stream without
                    # re-resolving the player every time.
                    if lesson_id.startswith("BLAW_EMBED_") and (media_url or item.get("subtitles") or item.get("subtitles_json")):
                        # Runtime lesson ids come in two shapes: BLAW_EMBED_<uuid>
                        # and BLAW_EMBED_<uuid>_<linkId8> (LTI deep links). Match
                        # either shape against uuid OR media_id — comparing the
                        # raw id (uuid_linkId) directly against the catalog uuid
                        # never matches, so resolved metadata silently no-oped.
                        embed_key = lesson_id.removeprefix("BLAW_EMBED_")
                        embed_uuid_part = embed_key.split("_", 1)[0] if "_" in embed_key else embed_key
                        embed_link_part = embed_key.split("_", 1)[1] if "_" in embed_key else ""
                        try:
                            from unisync.core.path_service import get_path_service
                            from unisync.core.file_io import atomic_write_json as _atomic_write_json
                            embed_path = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
                            embed_data = json.loads(embed_path.read_text(encoding="utf-8")) if embed_path.exists() else {}
                            changed = False
                            for _cid, _items in embed_data.items():
                              if _cid != "240718":
                                continue
                              for embed in _items or []:
                                _cat_uuid = str(embed.get("uuid") or embed.get("media_id") or "")
                                _cat_media = str(embed.get("media_id") or "")
                                _cat_link = str(embed.get("link_id") or "")
                                if embed_link_part:
                                    is_match = bool(_cat_link and _cat_link.startswith(embed_link_part))
                                else:
                                    is_match = (not _cat_link) and (
                                        embed_uuid_part in (_cat_uuid, _cat_media)
                                        or embed_key in (_cat_uuid, _cat_media)
                                        or (media_url and _cat_media and f"/{_cat_media}/" in media_url)
                                    )
                                    # Fallback: match unresolved embed with identical topic/title in syllabus item
                                    if not is_match and not embed.get("media_url") and embed.get("title") and title:
                                        clean_cat = re.sub(r"\(commentary\)\s*2026|2026|\.mp4", "", str(embed.get("title", "")), flags=re.I).strip().lower()
                                        clean_in = re.sub(r"\(commentary\)\s*2026|2026|\.mp4", "", str(title), flags=re.I).strip().lower()
                                        if clean_cat and (clean_cat == clean_in or clean_cat in clean_in or clean_in in clean_cat):
                                            is_match = True
                                if is_match:
                                    if media_url and embed.get("media_url") != media_url:
                                        embed["media_url"] = media_url
                                        embed["media_kind"] = media_kind
                                        changed = True
                                    # Update media_id with the real underlying media UUID if present in media_url
                                    if media_url:
                                        m_mid = re.search(r"/([a-f0-9\-]{36})/", media_url)
                                        if m_mid and embed.get("media_id") != m_mid.group(1):
                                            embed["media_id"] = m_mid.group(1)
                                            changed = True
                                    if item.get("subtitles") or item.get("subtitles_json"):
                                        embed["subtitle_available"] = True
                                        changed = True
                                    embed["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            if changed:
                                _atomic_write_json(embed_path, embed_data)
                                logger.info(
                                    "Merged resolved embed metadata into echo360_embeds.json for %s (media_url=%s subtitles=%s)",
                                    lesson_id, bool(media_url), bool(item.get("subtitles") or item.get("subtitles_json")),
                                )
                        except Exception as embed_meta_err:
                            logger.debug("Failed merging resolved embed metadata: %s", embed_meta_err)
                    
                    if lesson_id not in existing_ids and (media_url or item.get("subtitles") or item.get("subtitles_json")):
                        _cleanup_failed_echo360_id(lesson_id)
                        item_id = uuid4().hex
                        item_dir = _root() / item_id
                        item_dir.mkdir(parents=True, exist_ok=True)
                        
                        source_obj = Echo360Source(
                            id=lesson_id,
                            title=title,
                            date=lecture_date,
                            course_name=course_name,
                            course_id=sec_id,
                            media_url=media_url,
                            media_kind=media_kind,
                            cookie_header="",
                        )
                        
                        original_name = _safe_media_title(source_obj)
                        media_file = "media.mp4" if media_kind in ("mp4", "m3u8", "hls") else f"media.{media_kind}"
                        
                        meta = {
                            "id": item_id,
                            "title": title,
                            "original_name": original_name,
                            "content_type": "video/mp4" if media_kind in ("mp4", "m3u8", "hls") else "audio/mpeg",
                            "media_file": media_file,
                            "kb_name": kb_name,
                            "status": "queued",
                            "progress": "Queued from UniMelb Echo360",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "source": "echo360",
                            "source_id": lesson_id,
                            "source_url": f"https://echo360.net.au/lesson/{lesson_id}/classroom",
                            "institution": "University of Melbourne",
                            "course_id": sec_id,
                            "course_name": course_name,
                            "series_name": course_name,
                            "lecture_date": lecture_date,
                        }
                        _write(item_dir, meta)
                        
                        # P0-3: route through semaphore-throttled wrappers so at most
                        # _download_sem / _llm_sem tasks run concurrently, preventing
                        # OOM from 20×105MB simultaneous video downloads.
                        if media_url:
                            background_tasks.add_task(_throttled_download, _download_and_process_echo360, item_id, source_obj)
                        else:
                            background_tasks.add_task(_throttled_process, _process, item_id)
                            
                        existing_ids.add(lesson_id)
                        sec_queued += 1
                        total_queued += 1
                        date_str = f" ({lecture_date[:10]})" if lecture_date else ""
                        _record_diff(state, course_name, "NEW_LECTURE", f"+ NEW LECTURE: {title}{date_str}")
                        
                    elif lesson_id in existing_ids and media_url:
                        # Re-queue download for existing items missing their video file.
                        # This handles the case where subtitles were imported first but
                        # video URL resolution failed on a previous sync cycle.
                        for existing_dir in _root().iterdir():
                            if not existing_dir.is_dir() or not (existing_dir / "metadata.json").exists():
                                continue
                            emeta = _read(existing_dir)
                            if str(emeta.get("source_id")) != str(lesson_id):
                                continue
                            mf = emeta.get("media_file", "media.mp4")
                            media_path = existing_dir / mf
                            if media_path.exists() and media_path.stat().st_size > 0:
                                break  # already has video, skip
                            # Item exists but has no video — queue download
                            source_obj = Echo360Source(
                                id=lesson_id,
                                title=emeta.get("title", title),
                                date=lecture_date,
                                course_name=course_name,
                                course_id=sec_id,
                                media_url=media_url or "",
                                media_kind=media_kind or "mp4",
                                cookie_header="",
                            )
                            emeta["status"] = "queued"
                            emeta["progress"] = "Re-queued: downloading video"
                            _write(existing_dir, emeta)
                            # P0-3: use throttled wrapper for re-queue path too
                            background_tasks.add_task(
                                _throttled_download, _download_and_process_echo360, emeta["id"], source_obj
                            )
                            sec_queued += 1
                            total_queued += 1
                            break
                    
                    # Update delta state
                    sec_state["items"][lesson_id] = {
                        "title": title,
                        "date": lecture_date,
                        "type": "lecture",
                    }
                        
                if sub_count > 0:
                    log_sync(f"  - Downloaded {sub_count} subtitle tracks (.vtt) securely!", 30 + int(60 * (idx / max(1, len(sections)))), 100)
                if sec_queued > 0:
                    log_sync(f"  - Queued {sec_queued} lecture(s) for async download & RAG indexing!", 35 + int(60 * (idx / max(1, len(sections)))), 100)
          # P1-11: isolate per-section failures — partial sync completes
          except Exception as sec_err:
                sec_id = sec.get("sectionId", "<unknown>")
                log_sync(f"  ! Section {sec_id} failed: {sec_err}")
                logger.exception(f"P1-11: Section {sec_id} processing failed — continuing remaining sections")
                continue
                
        # P1-10: invalidate cache after successful queue loop so next poll rescans disk
        # Oracle fix: clear both include_failed keys
        for k in _existing_ids_cache:
            _existing_ids_cache[k]["at"] = 0
            
        _save_sync_state(state)
        if total_queued == 0 and total_subs == 0:
            log_sync(f"Hybrid Echo360 Sync Complete! All {len(sections)} sections up to date. (0 new recordings, zero compute spent)", 100, 100)
        else:
            log_sync(f"Hybrid Echo360 Sync Complete! ({total_subs} subtitles cached, {total_queued} lectures queued for download)", 100, 100)

        # Trigger automatic background Knowledge Base re-indexing when new subtitles/transcripts arrive
        if total_subs > 0:
            try:
                from unisync.services.kb.service import get_kb_service
                background_tasks.add_task(get_kb_service().index_all)
                logger.info("Queued background KB index update for %d new subtitle track(s)", total_subs)
            except Exception as _kb_err:
                logger.debug("Failed to schedule background KB reindex: %s", _kb_err)

        # Auto-trigger on-disk scrubber in background after new subtitles arrive
        if total_subs > 0:
            try:
                from unisync.services.disk_cleaner import clean_disk
                background_tasks.add_task(clean_disk)
            except Exception as _scrub_err:
                logger.debug("Failed to schedule background disk scrubber after Echo360 sync: %s", _scrub_err)
        
    sync_metadata["last_synced_at"] = datetime.now(timezone.utc).isoformat()

    # P0-3: protect sync_status mutation with lock
    async with _sync_lock:
        sync_status["is_syncing"] = False
    # P2-16: notify WS subscribers of terminal state (last log_sync may
    # have fired before the flag flipped)
    _schedule_broadcast()
    return {"status": "ok"}


@router.post("/clean-disk")
async def trigger_clean_disk() -> dict[str, Any]:
    """Trigger an on-demand background on-disk academic boilerplate scrub."""
    from unisync.services.disk_cleaner import run_background_disk_scrubber
    run_background_disk_scrubber(delay_sec=0.0)
    return {"status": "started", "message": "Background on-disk scrubber triggered successfully"}


@router.get("/changelog")
async def get_changelog() -> dict[str, Any]:
    """Returns recent course changes and diffs detected across sync cycles."""
    state = _load_sync_state()
    return {"diffs": list(reversed(state.get("recent_diffs", [])))}


# ---------------------------------------------------------------------------
# P0-3: Throttled wrappers — enforce semaphore limits so concurrent
# background_tasks.run callers cannot spawn unbounded I/O or LLM work.
# Each wrapper acquires its semaphore before forwarding to the real
# coroutine, then releases it automatically on completion / error.
# ---------------------------------------------------------------------------
async def _throttled_download(download_fn, item_id, source):
    """Download + RAG-index a single Echo360 lecture, gated by _download_sem.

    Accepts ``download_fn`` as a callable so callers (sync_payload) can pass
    the locally-imported ``_download_and_process_echo360`` without requiring
    a module-level import (which would cause circular-import issues with
    lectures.py).
    """
    async with _download_sem:
        await download_fn(item_id, source)

async def _throttled_process(process_fn, item_id):
    """Run post-download processing for a single lecture."""
    await process_fn(item_id)


@router.post("/start")
async def start_sync(background_tasks: BackgroundTasks, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Triggers the backend-native python sync engine (v3)."""
    
    # 1. Extract cookies from the jar
    canvas_cookies = get_cookies_for_domain("canvas.lms.unimelb.edu.au")
    echo_cookies = get_cookies_for_domain("echo360.net.au")
    
    if not canvas_cookies:
        logger.warning("Starting sync pipeline without Canvas cookies! It will likely fail to authenticate.")
        
    # 2. Fire the background task
    background_tasks.add_task(_run_sync_pipeline, canvas_cookies, echo_cookies)
    from unisync.services.sync.submissions import sync_submissions
    background_tasks.add_task(sync_submissions)
    
    logger.info("Started background-native sync task.")
    return {"status": "started"}


@router.post("/submissions")
async def start_submission_sync(background_tasks: BackgroundTasks, course_id: str | None = None):
    """Archive the current student's submissions; optional Canvas course ID filter."""
    from unisync.services.sync.submissions import sync_submissions
    background_tasks.add_task(sync_submissions, course_id)
    return {"status": "started"}


@router.get("/submissions/status")
async def submission_sync_status():
    from unisync.services.sync.submissions import sync_status as submissions_status
    return dict(submissions_status)

_CANVAS_FILE_BATCH_SIZE = 3


def _canvas_storage_for_course(course_id: str, course_name: str) -> tuple[Path, str]:
    """Return the registered KB directory for a Canvas course, creating a safe fallback."""
    from unisync.api.lectures import _ensure_kb_exists, _load_libraries
    from unisync.core.storage import current_kb_manager

    libraries = _load_libraries()
    entry = next(
        (
            value
            for key, value in libraries.items()
            if str(key) == course_id or str(value.get("canvas_course_id") or "") == course_id
        ),
        {},
    )
    kb_name = str(entry.get("kb_name") or f"canvas_course_{course_id}")
    _ensure_kb_exists(kb_name, description=f"Knowledge base for Canvas course {course_name}")
    return current_kb_manager().get_knowledge_base_path(kb_name), kb_name


def _find_lecture_capture_url(tabs: list[dict[str, Any]]) -> str:
    for tab in tabs:
        label = f"{tab.get('label') or ''} {tab.get('id') or ''}".lower()
        if "echo360" not in label and "lecture capture" not in label:
            continue
        url = str(tab.get("html_url") or "")
        if url.startswith("/"):
            return "https://canvas.lms.unimelb.edu.au" + url
        if url.startswith("http"):
            return url
    return ""


def _canvas_course_auto_sync_enabled(course_id: str) -> bool:
    """Match the previous bridge rule: only an explicit auto_sync=false opts out."""
    from unisync.api.lectures import _load_libraries

    for key, entry in _load_libraries().items():
        if str(key) == course_id or str(entry.get("canvas_course_id") or "") == course_id:
            return entry.get("auto_sync") is not False
    return True


def _canvas_echo_urls(value: object) -> list[str]:
    """Extract Echo360 launch/player URLs from raw or Canvas-wrapped HTML."""
    import re

    if not isinstance(value, str) or not value:
        return []
    variants = {value}
    for _ in range(2):
        variants.update(html.unescape(unquote(candidate)) for candidate in tuple(variants))
    found: set[str] = set()
    pattern = re.compile(
        r"(?:https?:)?//echo360\.net\.au/(?:media|lti|ui/player)/[a-f0-9-]+(?:\?[^\s\"'<>]*)?",
        re.IGNORECASE,
    )
    for candidate in variants:
        found.update(match.group(0) for match in pattern.finditer(candidate))
    return sorted(found)


def _echo_embeds_path() -> Path:
    from unisync.core.path_service import get_path_service

    path = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_echo_embeds() -> dict[str, list[dict[str, Any]]]:
    path = _echo_embeds_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): [item for item in value if isinstance(item, dict)]
        for key, value in data.items()
        if isinstance(value, list)
    }


def _persist_canvas_echo_embeds(course_id: str, embedded_items: list[dict[str, str]]) -> int:
    """Store Canvas page/module Echo360 links under the canonical per-link identity.

    One Canvas external-tool UUID can serve many distinct ``linkId`` videos, so
    the tool UUID alone must never identify an embed. Existing rows (including
    richer metadata written by the legacy bridge) are merged, never replaced.
    """
    from unisync.core.file_io import atomic_write_json
    from unisync.services.sync.echo360_ingest import (
        link_id_from_url,
        media_uuid_from_url,
        merge_canvas_embeds,
        utc_now,
    )

    data = _load_echo_embeds()
    records = data.get(str(course_id), [])
    discovered: list[dict[str, Any]] = []
    for item in embedded_items:
        raw_url = str(item.get("url") or "").strip()
        uuid_value = media_uuid_from_url(raw_url)
        if not uuid_value:
            continue
        parsed = urlparse(raw_url if raw_url.startswith("http") else "https:" + raw_url)
        query = parse_qs(parsed.query)
        link_id = link_id_from_url(raw_url) or next(iter(query.get("linkId", [])), None)
        media_id = next(iter(query.get("currentMediaId", [])), uuid_value)
        secure_id = next(iter(query.get("secureLinkAccessDataId", [])), None)
        title = str(item.get("title") or "").strip()
        if link_id:
            canvas_launch = (
                f"https://canvas.lms.unimelb.edu.au/courses/{course_id}/external_tools/retrieve"
                f"?display=borderless&url={quote(raw_url, safe='')}"
            )
        elif "/ui/player/" in parsed.path:
            canvas_launch = raw_url
        else:
            canvas_launch = f"https://echo360.net.au/media/{uuid_value}/public"
        discovered.append(
            {
                "provider": "echo360",
                "title": title or "Echo360 Video",
                "source_title": title or "Canvas Page Video",
                "source_kind": item.get("source_kind") or "page",
                "provider_launch_url": raw_url,
                "canvas_launch_url": canvas_launch,
                "player_url": raw_url,
                "uuid": uuid_value,
                "media_id": media_id,
                "link_id": link_id,
                "secure_link_access_data_id": secure_id,
                "resolved_at": utc_now(),
            }
        )
    merged, added, updated = merge_canvas_embeds(records, discovered, course_id=course_id)
    if added or updated:
        data[str(course_id)] = merged
        atomic_write_json(_echo_embeds_path(), data)
    return added


def _enrich_echo_embeds_from_syllabus(sections: list[dict[str, Any]]) -> int:
    """Attach syllabus lecture dates to embeds whose titles match exactly."""
    from unisync.core.file_io import atomic_write_json
    from unisync.services.sync.echo360_ingest import enrich_embed_from_syllabus, syllabus_lesson_index

    index = syllabus_lesson_index(sections)
    if not index:
        return 0
    data = _load_echo_embeds()
    changed = 0
    for records in data.values():
        for record in records:
            if enrich_embed_from_syllabus(record, index):
                changed += 1
    if changed:
        atomic_write_json(_echo_embeds_path(), data)
    return changed


async def _index_canvas_materials() -> dict[str, Any]:
    """Reindex snapshots and downloads once the native Canvas pass has written them."""
    from unisync.services.kb import get_kb_service

    service = get_kb_service()
    result: dict[str, Any] = {}
    for attempt in range(6):
        result = await asyncio.to_thread(service.index_all)
        if result.get("status") != "busy-skipped":
            return result
        if attempt < 5:
            await asyncio.sleep(2)
    return result


def _sync_canvas_course_delta(course_id: str, course_name: str, modules: list[dict[str, Any]], assignments: list[dict[str, Any]]) -> None:
    """Maintain the change feed and due-date todos previously written by the bridge."""
    state = _load_sync_state()
    course_state = state.setdefault("courses", {}).setdefault(
        course_id, {"name": course_name, "items": {}}
    )
    course_state["name"] = course_name
    items_state = course_state.setdefault("items", {})
    todo_service = None

    def record(
        item_id: object,
        title: str,
        kind: str,
        due_at: str,
        module_name: str = "",
        *,
        create_todo: bool = True,
    ) -> None:
        nonlocal todo_service
        key = str(item_id or title)
        previous = items_state.get(key)
        if not previous:
            _record_diff(state, course_name, "NEW_ITEM", f"+ NEW: {title} ({kind})")
        elif due_at and previous.get("due_at") and previous["due_at"] != due_at:
            _record_diff(
                state,
                course_name,
                "DUE_SHIFT",
                f"~ DUE: {title} shifted from {previous['due_at'][:10]} -> {due_at[:10]}",
            )
        items_state[key] = {"title": title, "type": kind, "due_at": due_at, "module": module_name}
        if due_at and create_todo:
            try:
                if todo_service is None:
                    from unisync.services.todos import get_todo_service
                    todo_service = get_todo_service()
                source_id = f"canvas:{course_id}:{key}"
                existing = next(
                    (item for item in todo_service.list_items() if item.source_id == source_id), None
                )
                if existing:
                    if previous and previous.get("due_at") != due_at:
                        todo_service.update_item(existing.id, {"due_at": due_at})
                else:
                    lower_kind = kind.lower()
                    todo_kind = "assignment" if "assign" in lower_kind else "exam" if "quiz" in lower_kind or "exam" in lower_kind else "admin"
                    todo_service.create_item(
                        {
                            "title": title,
                            "due_at": due_at,
                            "kind": todo_kind,
                            "course": course_name,
                            "priority": "high" if todo_kind == "exam" else "normal",
                            "notes": f"Canvas {kind}" + (f": {module_name}" if module_name else ""),
                            "source": "canvas",
                            "source_id": source_id,
                        }
                    )
            except Exception as exc:
                logger.debug("Failed to update Canvas due-date todo (%s)", type(exc).__name__)
    for module in modules:
        module_name = str(module.get("name") or "Module")
        for item in module.get("items") or []:
            if not isinstance(item, dict):
                continue
            record(
                item.get("id") or item.get("title"),
                str(item.get("title") or "Item"),
                str(item.get("type") or "Page"),
                str(item.get("due_at") or ""),
                module_name,
            )
    for assignment in assignments:
        assignment_id = assignment.get("id")
        if assignment_id:
            record(
                f"assignment:{assignment_id}",
                str(assignment.get("name") or "Assignment"),
                "Assignment",
                str(assignment.get("due_at") or ""),
                create_todo=False,
            )
    _save_sync_state(state)


def _update_canvas_library_metadata(course_id: str, lecture_capture_url: str) -> None:
    """Keep the non-secret course registry in step with a native Canvas refresh."""
    from unisync.api.lectures import _load_libraries, _save_libraries

    libraries = _load_libraries()
    changed = False
    for key, entry in libraries.items():
        if str(key) != course_id and str(entry.get("canvas_course_id") or "") != course_id:
            continue
        entry["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        if lecture_capture_url:
            entry["lecture_capture_url"] = lecture_capture_url
        changed = True
    if changed:
        _save_libraries(libraries)


def _canvas_attachment_candidates(
    assignments: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    pages: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Collect the same linked-file sources the legacy Canvas crawler covered."""
    from unisync.services.sync.canvas_snapshot import extract_canvas_file_ids

    candidates: dict[str, dict[str, str]] = {}

    def add(file_id: object, *, title: object = "", folder: str = "canvas") -> None:
        file_id = str(file_id or "").strip()
        if file_id:
            candidates.setdefault(file_id, {"title": str(title or ""), "folder": folder})

    for assignment in assignments:
        title = assignment.get("name") or "assignment"
        for attachment in assignment.get("attachments") or []:
            if isinstance(attachment, dict):
                add(
                    attachment.get("id"),
                    title=attachment.get("display_name") or attachment.get("filename"),
                    folder=f"assignments/{title}",
                )
        for file_id in extract_canvas_file_ids(assignment.get("description")):
            add(file_id, folder=f"assignments/{title}")

    for module in modules:
        module_name = module.get("name") or "module"
        for item in module.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "File":
                add(
                    item.get("content_id") or item.get("id"),
                    title=item.get("title"),
                    folder=f"modules/{module_name}",
                )

    for page in pages:
        page_title = page.get("title") or "page"
        for file_id in extract_canvas_file_ids(page.get("body")):
            add(file_id, folder=f"pages/{page_title}")
    return candidates


def _safe_canvas_relative_folder(value: str) -> Path:
    """Convert a Canvas display path to safe local path components."""
    from unisync.core.validator import DocumentValidator

    pieces = []
    for part in value.replace("\\", "/").split("/"):
        if part and part not in {".", ".."}:
            pieces.append(DocumentValidator.validate_upload_safety(part, None))
    return Path(*pieces) if pieces else Path("canvas")


async def _record_native_canvas_file(
    *,
    course_id: str,
    course_name: str,
    kb_name: str,
    file_id: str,
    display_name: str,
    content_type: str,
    destination: Path,
    task_id: str,
) -> None:
    """Mark a native Canvas download as complete only after it exists on disk."""
    from unisync.core.path_service import get_path_service

    kb_path = get_path_service().get_knowledge_base_path(kb_name)
    try:
        relative_path = str(destination.relative_to(kb_path))
    except ValueError:
        relative_path = str(destination)
    async with _sync_lock:
        manifest = _load_canvas_files()
        entry = manifest.setdefault(course_id, {"kb_name": kb_name, "course_name": course_name, "files": {}})
        entry["kb_name"] = kb_name
        entry["course_name"] = course_name
        entry.setdefault("files", {})[file_id] = {
            "display_name": display_name,
            "size": destination.stat().st_size,
            "content_type": content_type or "application/octet-stream",
            "kb_path": relative_path,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
        }
        _save_canvas_files(manifest)


async def _download_canvas_attachments(
    canvas,
    *,
    course_id: str,
    course_name: str,
    course_dir: Path,
    kb_name: str,
    candidates: dict[str, dict[str, str]],
) -> list[Path]:
    """Download every linked course file in small batches through the resident session."""
    from unisync.core.validator import DocumentValidator
    from uuid import uuid4

    existing = _load_canvas_files().get(course_id, {}).get("files", {})
    pending = [(file_id, data) for file_id, data in candidates.items() if file_id not in existing]
    if not pending:
        return []

    task_id = f"canvas_files_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    downloaded: list[Path] = []

    async def download_one(file_id: str, candidate: dict[str, str]) -> Path | None:
        try:
            details = await canvas.get_file(file_id)
            if not details:
                return None
            download_url = str(details.get("url") or "")
            if not download_url:
                return None
            original_name = str(details.get("display_name") or details.get("filename") or candidate["title"] or f"canvas_file_{file_id}")
            filename = DocumentValidator.validate_upload_safety(Path(original_name).name, None)
            destination = course_dir / "raw" / "canvas" / course_id / _safe_canvas_relative_folder(candidate["folder"]) / filename
            content_type = str(details.get("content-type") or details.get("content_type") or "application/octet-stream")
            ok = await canvas.download_file(
                download_url,
                destination,
                item_id=file_id,
                course_id=course_id,
                course_name=course_name,
                content_type=content_type,
                metadata={"folder": candidate["folder"], "kb_name": kb_name, "task_id": task_id},
            )
            if not ok:
                return None
            await _record_native_canvas_file(
                course_id=course_id,
                course_name=course_name,
                kb_name=kb_name,
                file_id=file_id,
                display_name=original_name,
                content_type=content_type,
                destination=destination,
                task_id=task_id,
            )
            return destination
        except Exception as exc:
            logger.debug("Canvas attachment %s failed (%s)", file_id, type(exc).__name__)
            return None

    for offset in range(0, len(pending), _CANVAS_FILE_BATCH_SIZE):
        batch = pending[offset : offset + _CANVAS_FILE_BATCH_SIZE]
        results = await asyncio.gather(
            *(download_one(file_id, candidate) for file_id, candidate in batch)
        )
        downloaded.extend(path for path in results if path is not None)
        if offset + _CANVAS_FILE_BATCH_SIZE < len(pending):
            await asyncio.sleep(0.1)
    return downloaded


async def _sync_native_canvas_course(
    canvas,
    course: dict[str, Any],
    *,
    modules: list[dict[str, Any]] | None = None,
    assignments: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Persist one Canvas course using only the authenticated Camoufox session."""
    from unisync.services.sync.canvas_snapshot import CanvasSnapshotWriter

    course_id = str(course.get("id") or "")
    if not course_id:
        return {"snapshots": 0, "attachments": 0}
    course_name = str(course.get("name") or f"Course {course_id}")
    modules = modules if modules is not None else await canvas.get_course_modules(course_id)
    assignments = assignments if assignments is not None else await canvas.get_course_assignments(course_id)
    _sync_canvas_course_delta(course_id, course_name, modules, assignments)
    tabs = await canvas.get_course_tabs(course_id)
    pages = await canvas.get_course_pages(course_id)
    discussions = await canvas.get_course_discussions(course_id)
    announcements = await canvas.get_course_announcements(course_id)
    quizzes = await canvas.get_course_quizzes(course_id)

    course_dir, kb_name = _canvas_storage_for_course(course_id, course_name)
    writer = CanvasSnapshotWriter(course_dir, course_id)
    snapshots = int(writer.write_overview(course)) + int(writer.write_modules(modules))
    for assignment in assignments:
        _upsert_canvas_assignment(course_id, course_name, assignment)
        snapshots += int(writer.write_assignment(assignment))
    for page in pages:
        snapshots += int(writer.write_page(page))
    snapshots += writer.write_collection("discussions", discussions)
    snapshots += writer.write_collection("announcements", announcements)
    snapshots += writer.write_collection("quizzes", quizzes)

    echo_candidates: list[dict[str, str]] = []
    for page in pages:
        title = str(page.get("title") or "")
        echo_candidates.extend(
            {"url": url, "title": title, "source_kind": "page"}
            for url in _canvas_echo_urls(page.get("body"))
        )
    for assignment in assignments:
        title = str(assignment.get("name") or "")
        echo_candidates.extend(
            {"url": url, "title": title, "source_kind": "assignment"}
            for url in _canvas_echo_urls(assignment.get("description"))
        )
    for module in modules:
        for item in module.get("items") or []:
            if not isinstance(item, dict):
                continue
            for url in _canvas_echo_urls(item.get("html_url") or item.get("url")):
                echo_candidates.append(
                    {"url": url, "title": str(item.get("title") or ""), "source_kind": "module"}
                )
    echo_embeds_added = _persist_canvas_echo_embeds(course_id, echo_candidates)

    attachments = await _download_canvas_attachments(
        canvas,
        course_id=course_id,
        course_name=course_name,
        course_dir=course_dir,
        kb_name=kb_name,
        candidates=_canvas_attachment_candidates(assignments, modules, pages),
    )
    lecture_capture_url = _find_lecture_capture_url(tabs)
    _update_canvas_library_metadata(course_id, lecture_capture_url)
    return {"snapshots": snapshots, "attachments": len(attachments), "echo_embeds": echo_embeds_added}


@router.post("/courses/{course_id}/refresh")
async def refresh_canvas_course(course_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Refresh one course without requiring the deprecated browser bridge."""
    async with _sync_lock:
        if sync_status.get("is_syncing"):
            return {"status": "already_syncing"}
        sync_status["is_syncing"] = True
    background_tasks.add_task(_run_single_canvas_refresh, str(course_id))
    return {"status": "started", "course_id": str(course_id)}


async def _run_single_canvas_refresh(course_id: str) -> None:
    from unisync.services.sync.canvas_client import CanvasClient

    canvas = CanvasClient("https://canvas.lms.unimelb.edu.au", get_cookies_for_domain("canvas.lms.unimelb.edu.au"))
    try:
        async with _sync_lock:
            sync_status["is_syncing"] = True
            sync_status["log"].clear()
        courses = await canvas.get_courses()
        course = next((item for item in courses if str(item.get("id")) == course_id), None)
        if not course:
            log_sync(f"Canvas course {course_id} is not available in the active enrolments.", 100, 100)
            return
        log_sync(f"Refreshing Canvas course: {course.get('name') or course_id}", 0, 100)
        result = await _sync_native_canvas_course(canvas, course)
        index_result = await _index_canvas_materials()
        log_sync(
            f"Canvas course refresh complete: {result['snapshots']} snapshot(s), {result['attachments']} attachment(s), {result['echo_embeds']} Echo360 link(s); indexed {index_result.get('files_indexed', 0)} file(s).",
            100,
            100,
        )
    except Exception as exc:
        logger.exception("Targeted Canvas refresh failed for %s", course_id)
        log_sync(f"Canvas course refresh failed ({type(exc).__name__}).", 100, 100)
    finally:
        async with _sync_lock:
            sync_status["is_syncing"] = False
        _schedule_broadcast()
        await canvas.close()


async def _run_sync_pipeline(canvas_cookies: dict, echo_cookies: dict):
    """Camoufox-native Canvas sync, retaining the prior bridge's Canvas coverage."""
    from unisync.services.sync.canvas_client import CanvasClient
    from unisync.services.sync.echo360_client import Echo360Client

    canvas = CanvasClient("https://canvas.lms.unimelb.edu.au", canvas_cookies)
    echo = Echo360Client("https://echo360.net.au", echo_cookies)
    try:
        async with _sync_lock:
            sync_status["is_syncing"] = True
            sync_status["log"].clear()
        log_sync("Camoufox-native sync starting...", 0, 100)

        from unisync.services.browser import get_camoufox_service

        session = await get_camoufox_service().background_sync_session()
        if not session.get("authenticated"):
            if session.get("login_required"):
                from unisync.api.browser import trigger_interactive_login

                await trigger_interactive_login()
            log_sync(
                "Canvas login required. Native sync is queued and will resume after authentication.",
                0,
                100,
            )

            # A run must always record an outcome: never leave the persisted
            # state saying "never", and keep pending work queued in place.
            from unisync.services.sync.native_sync import NativeSyncState

            _native_state = NativeSyncState()
            _login_required = bool(session.get("login_required"))
            _native_state.finish_run(
                "login_required" if _login_required else "auth_unknown",
                error="Canvas/Echo360 session not authenticated",
                login_required=_login_required,
            )
            if not _login_required:
                # No interactive login was opened, so retry on the next tick.
                _native_state.data["next_run_at"] = 0.0
                _native_state.save()
            return

        courses = [
            course
            for course in await canvas.get_courses()
            if _canvas_course_auto_sync_enabled(str(course.get("id") or ""))
        ]
        course_ids = [str(course["id"]) for course in courses if course.get("id")]
        log_sync(f"Discovered {len(courses)} Canvas courses.", 5, 100)
        modules_by_course = await canvas.get_all_modules(course_ids)
        assignments_by_course = await canvas.get_all_course_assignments(course_ids)

        for index, course in enumerate(courses):
            course_id = str(course.get("id") or "")
            course_name = str(course.get("name") or course_id)
            progress = 10 + int(55 * (index / max(1, len(courses))))
            log_sync(f"Syncing Canvas course: {course_name}", progress, 100)
            result = await _sync_native_canvas_course(
                canvas,
                course,
                modules=modules_by_course.get(course_id, []),
                assignments=assignments_by_course.get(course_id, []),
            )
            log_sync(
                f"[{course_name}] saved {result['snapshots']} snapshot(s), {result['attachments']} attachment(s), and {result['echo_embeds']} Echo360 link(s).",
                progress,
                100,
            )

        index_result = await _index_canvas_materials()
        log_sync(f"Indexed Canvas material: {index_result.get('files_indexed', 0)} file(s).", 68, 100)

        sections = await echo.get_user_sections()
        log_sync(f"Discovered {len(sections)} Echo360 sections.", 70, 100)
        section_ids = [str(section["sectionId"]) for section in sections if section.get("sectionId")]
        syllabi = await echo.get_all_syllabi(section_ids)

        from unisync.core.path_service import get_path_service
        from unisync.services.sync.echo360_ingest import persist_syllabus

        syllabus_path = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
        section_names = {
            str(section.get("sectionId")): str(section.get("name") or section.get("sectionName") or "")
            for section in sections
            if section.get("sectionId")
        }
        persisted = persist_syllabus(syllabus_path, syllabi, section_names=section_names)
        enriched = _enrich_echo_embeds_from_syllabus(persisted)
        log_sync(
            f"Persisted Echo360 syllabus for {len(persisted)} section(s); enriched {enriched} embed date(s).",
            85,
            100,
        )
        native_result = await _native_echo360_pass()
        log_sync(
            f"Native Echo360 pass: {native_result.get('status')} "
            f"({native_result.get('processed', 0)} processed, {native_result.get('transcripts', 0)} transcript(s), "
            f"{native_result.get('pending', 0)} pending).",
            95,
            100,
        )
        sync_metadata["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        log_sync("Camoufox-native sync complete.", 100, 100)
    except Exception as exc:
        logger.exception("Native sync pipeline failed")
        try:
            from unisync.services.sync.native_sync import NativeSyncState

            NativeSyncState().finish_run("failed", error=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:
            pass
        log_sync(f"Native sync failed ({type(exc).__name__}).", 100, 100)
    finally:
        async with _sync_lock:
            sync_status["is_syncing"] = False
        _schedule_broadcast()
        await canvas.close()
        await echo.close()

def _section_owner_map(libraries: dict[str, Any]) -> dict[str, str]:
    """Map Echo360 section id -> Canvas course id from the course registry."""
    owners: dict[str, str] = {}
    for key, entry in libraries.items():
        if not isinstance(entry, dict):
            continue
        canvas_id = str(entry.get("canvas_course_id") or key or "").strip()
        section_id = str(entry.get("echo360_course_id") or "").strip()
        if canvas_id and section_id:
            owners[section_id] = canvas_id
    return owners


def _load_syllabus_sections(path: Path) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def _record_echo_coverage(
    state,
    targets: dict[str, list[dict[str, Any]]],
    *,
    cache_dir: Path,
    thumbnail_dir: Path,
) -> None:
    """Store per-course coverage backed by current disk state, not cache counts.

    Both ingestion paths are reported side by side: Canvas page/module embeds
    (``build_coverage``) and Echo360 direct syllabus lessons (``build_echo360_coverage``).
    """
    from unisync.api.lectures import _load_libraries
    from unisync.core.path_service import get_path_service
    from unisync.services.sync.echo360_ingest import build_echo360_coverage

    root = get_path_service().get_user_root()
    catalog_path = root / "libraries" / "echo360_embeds.json"
    syllabus_path = root / "cache" / "echo360_syllabus.json"
    catalog_at = (
        datetime.fromtimestamp(catalog_path.stat().st_mtime, timezone.utc).isoformat()
        if catalog_path.exists()
        else ""
    )
    syllabus_at = (
        datetime.fromtimestamp(syllabus_path.stat().st_mtime, timezone.utc).isoformat()
        if syllabus_path.exists()
        else ""
    )

    libraries = _load_libraries()
    owners = _section_owner_map(libraries)
    course_names = {
        str(entry.get("canvas_course_id") or key): str(entry.get("name") or "")
        for key, entry in libraries.items()
        if isinstance(entry, dict) and str(entry.get("canvas_course_id") or key)
    }
    sections_by_course: dict[str, list[dict[str, Any]]] = {}
    for section in _load_syllabus_sections(syllabus_path):
        owner = owners.get(str(section.get("sectionId") or section.get("id") or ""))
        if owner:
            sections_by_course.setdefault(owner, []).append(section)

    # A course can be covered entirely by the Echo360 syllabus and have no
    # Canvas embeds at all (CCDP10003), so report the union of both sources.
    for course_id in sorted(set(targets) | set(sections_by_course)):
        key = str(course_id)
        records = targets.get(course_id, [])
        direct = build_echo360_coverage(
            sections_by_course.get(key, []),
            course_id=key,
            course_name=course_names.get(key, ""),
            cache_dir=cache_dir,
            syllabus_fetched_at=syllabus_at,
        )
        state.record_coverage(
            key,
            records,
            cache_dir=cache_dir,
            thumbnail_dir=thumbnail_dir,
            catalog_updated_at=catalog_at,
            syllabus_fetched_at=syllabus_at,
            course_name=course_names.get(key, ""),
            direct=direct,
        )


async def _native_echo360_pass(
    course_ids: list[str] | None = None,
    *,
    max_items: int = 40,
) -> dict[str, Any]:
    """Resolve queued Echo360 transcripts/media through the resident browser.

    Work is persisted before and after every attempt, so an expired session or a
    crash leaves the remaining corpus queued for the next run. A transcript is
    only counted after a cue-valid WebVTT write for the canonical per-link key.
    """
    from unisync.core.file_io import atomic_write_json
    from unisync.core.path_service import get_path_service
    from unisync.services.browser import get_camoufox_service
    from unisync.services.sync.echo360_ingest import (
        canonical_embed_key,
        embed_transcript_path,
        embed_transcript_present,
    )
    from unisync.services.sync.echo360_native import Echo360NativeIngestor, ingest_many
    from unisync.services.sync.native_sync import NativeSyncState

    wanted = {str(course_id) for course_id in (course_ids or []) if str(course_id).strip()}
    root = get_path_service().get_user_root()
    cache_dir = root / "cache" / "echo360_subtitles"
    thumbnail_dir = root / "cache" / "echo360_thumbnails"

    catalog = _load_echo_embeds()
    # Coverage is recomputed for every course: a per-course refresh must never
    # zero the counters of courses it did not touch.
    coverage_targets: dict[str, list[dict[str, Any]]] = {}
    targets: dict[str, list[dict[str, Any]]] = {}
    for cid, items in catalog.items():
        for record in items:
            record["canvas_course_id"] = str(cid)
        coverage_targets[str(cid)] = items
        if wanted and str(cid) not in wanted:
            continue
        targets[str(cid)] = items

    records = [record for items in targets.values() for record in items]
    state = NativeSyncState()
    if not records:
        state.finish_run("empty")
        return {"status": "empty", "processed": 0, "transcripts": 0, "pending": 0}

    queued = state.enqueue(records, cache_dir=cache_dir, thumbnail_dir=thumbnail_dir)
    if not queued:
        _record_echo_coverage(
            state, coverage_targets, cache_dir=cache_dir, thumbnail_dir=thumbnail_dir
        )
        state.finish_run("up_to_date")
        return {"status": "up_to_date", "processed": 0, "transcripts": 0, "pending": 0}

    service = get_camoufox_service()
    try:
        session = await service.background_sync_session()
    except Exception as exc:
        logger.info("Native Echo360 session check unavailable (%s)", type(exc).__name__)
        session = {"authenticated": False, "login_required": False}

    if not session.get("authenticated"):
        login_required = bool(session.get("login_required"))
        if login_required:
            from unisync.api.browser import trigger_interactive_login

            await trigger_interactive_login()
        status = "login_required" if login_required else "auth_unknown"
        state.finish_run(
            status,
            error="Canvas/Echo360 session not authenticated",
            login_required=login_required,
        )
        return {"status": status, "processed": 0, "transcripts": 0, "pending": len(queued)}

    state.begin_run()
    ingestor = Echo360NativeIngestor(service=service, cache_dir=cache_dir, thumbnail_dir=thumbnail_dir)
    outcomes = await ingest_many(ingestor, queued[:max_items], concurrency=2)

    by_key = {
        canonical_embed_key(
            record.get("canvas_course_id"),
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        ): record
        for record in records
    }
    changed = False
    transcripts = 0
    for outcome in outcomes:
        record = by_key.get(outcome.key)
        if record is not None:
            # Persist what the browser resolved so a later run goes straight to
            # the playback id instead of guessing from the LTI tool uuid.
            if outcome.media_id:
                record["resolved_media_id"] = outcome.media_id
                changed = True
            if outcome.media_url:
                record["media_url"] = outcome.media_url
                record["media_kind"] = outcome.media_kind or record.get("media_kind") or "m3u8"
                changed = True
            # Coverage must report the last real attempt, not a stale guess.
            record["last_outcome_status"] = outcome.status
            record["last_outcome_reason"] = outcome.reason
            changed = True
        present = outcome.status == "transcript" or (
            record is not None and embed_transcript_present(cache_dir, record)
        )
        if present:
            transcripts += 1
        if record is not None:
            record["subtitle_available"] = bool(present)
            if present:
                transcript = embed_transcript_path(cache_dir, record)
                if transcript is not None:
                    record["subtitle_path"] = str(transcript)
            changed = True
        state.note_outcome(outcome.key, outcome.as_dict(), transcript_present=present)

    if changed:
        atomic_write_json(_echo_embeds_path(), catalog)

    _record_echo_coverage(
        state, coverage_targets, cache_dir=cache_dir, thumbnail_dir=thumbnail_dir
    )
    status = "complete" if transcripts == len(outcomes) else "partial"
    state.finish_run(status)
    return {
        "status": status,
        "processed": len(outcomes),
        "transcripts": transcripts,
        "pending": len(state.pending_keys()),
    }


async def _run_native_course_refresh(course_ids: list[str]) -> None:
    try:
        async with _sync_lock:
            sync_status["is_syncing"] = True
            sync_status["log"].clear()
        log_sync(f"Native Echo360 refresh for {len(course_ids)} course(s)...", 0, 100)
        result = await _native_echo360_pass(course_ids)
        log_sync(
            f"Native Echo360 refresh {result.get('status')}: {result.get('transcripts', 0)} transcript(s), "
            f"{result.get('pending', 0)} pending.",
            100,
            100,
        )
    except Exception as exc:
        logger.exception("Native course refresh failed")
        log_sync(f"Native course refresh failed ({type(exc).__name__}).", 100, 100)
    finally:
        async with _sync_lock:
            sync_status["is_syncing"] = False
        _schedule_broadcast()


async def native_sync_scheduler() -> None:
    """Startup + periodic native sync once a verified session is available."""
    from unisync.services.sync.native_sync import NativeSyncState

    # Let session maintenance restore/lock the profile before the first pass.
    await asyncio.sleep(20)
    while True:
        try:
            state = NativeSyncState()
            if state.due() and not sync_status.get("is_syncing"):
                await _run_sync_pipeline({}, {})
        except Exception as exc:
            logger.info("Native sync scheduler unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(60)


@router.get("/native/status")
async def native_sync_status() -> dict[str, Any]:
    """Honest scheduler/queue state, separate from catalog freshness."""
    from unisync.services.sync.native_sync import NativeSyncState

    return NativeSyncState().summary()


@router.get("/native/coverage")
async def native_sync_coverage() -> dict[str, Any]:
    """Per-course discovered/resolved/transcript coverage and pending items."""
    from unisync.services.sync.native_sync import NativeSyncState

    summary = NativeSyncState().summary()
    return {
        "courses": summary.get("courses", {}),
        "totals": summary.get("totals", {}),
        "pending": summary.get("pending", []),
        "last_run_status": summary.get("last_run_status"),
        "login_required": summary.get("login_required"),
    }


@router.post("/native/schedule")
async def configure_native_schedule(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    from unisync.services.sync.native_sync import NativeSyncState

    state = NativeSyncState()
    state.configure(enabled=body.get("enabled"), interval_seconds=body.get("interval_seconds"))
    return state.summary()


@router.post("/native/refresh")
async def native_refresh(
    background_tasks: BackgroundTasks,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Manual full sync or per-course Echo360 refresh; no open browser tab needed."""
    course_ids = [
        str(course_id) for course_id in (body.get("course_ids") or []) if str(course_id).strip()
    ]
    async with _sync_lock:
        if sync_status.get("is_syncing"):
            return {"status": "already_syncing"}
    if course_ids:
        background_tasks.add_task(_run_native_course_refresh, course_ids)
        return {"status": "started", "scope": "courses", "course_ids": course_ids}
    background_tasks.add_task(_run_sync_pipeline, {}, {})
    return {"status": "started", "scope": "full"}


def get_cookies_for_domain(domain: str) -> dict[str, str]:
    """Returns a dict of cookies applicable to the given domain."""
    return {
        name: value 
        for (d, name), value in global_cookie_jar.items() 
        if domain in d or d in domain
    }
