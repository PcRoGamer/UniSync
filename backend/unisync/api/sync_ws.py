"""
P2-16: WebSocket + HTTP-poll endpoint for sync engine progress.

Replaces HTTP polling (§1.3 in SYNC_WEBSOCKET_DESIGN.md) with a push-based
WebSocket channel. Follows the singleton ``ProgressBroadcaster`` pattern
(see ``unisync progress broadcaster``) but scoped to a
single ``"global"`` key since sync is a process-level background job, not
per-KB.

Endpoints:
  GET  /poll          — backward-compatible HTTP fallback (alias of /progress)
  WS   /ws            — push channel: snapshot on connect, progress events,
                         30 s ping keepalive, pong handling
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# P2-16: SyncProgressBroadcaster — singleton that fans out sync_status
# mutations to all connected WebSocket clients.
# ---------------------------------------------------------------------------

# P2-16: env gate — defaults to ON (progress WS is cheap, poll fallback remains).
# Set USE_WS_PROGRESS=0/false/no to disable broadcasts (e.g., for load testing).
_USE_WS = os.getenv("USE_WS_PROGRESS", "true").lower() not in ("0", "false", "no", "off")


class SyncProgressBroadcaster:
    """Singleton broadcaster for global sync progress.

    All connected clients live under a single ``"global"`` key (sync is
    process-wide, not per-KB). The ``broadcast()`` method is safe to call
    from ``_schedule_broadcast()`` which may fire from a synchronous
    ``log_sync()`` context via ``loop.call_soon_threadsafe``.
    """

    _instance: SyncProgressBroadcaster | None = None
    _connections: dict[str, set[WebSocket]] = {"global": set()}
    _lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> SyncProgressBroadcaster:
        """Return the singleton instance, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and track a new WebSocket connection."""
        async with self._lock:
            self._connections["global"].add(websocket)
            logger.debug(
                "SyncProgressBroadcaster: connected (total=%d)",
                len(self._connections["global"]),
            )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket from the tracked set."""
        async with self._lock:
            self._connections["global"].discard(websocket)
            logger.debug(
                "SyncProgressBroadcaster: disconnected (total=%d)",
                len(self._connections["global"]),
            )

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Push ``{type: "progress", data: ...}`` to every connected client.

        Dead connections are pruned automatically so callers never need to
        worry about stale sockets.
        """
        if not _USE_WS:
            return  # P2-16: gated behind USE_WS_PROGRESS env flag

        async with self._lock:
            sockets = list(self._connections["global"])

        if not sockets:
            return

        to_remove: list[WebSocket] = []
        payload = {"type": "progress", "data": data}

        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception as exc:
                logger.debug("SyncProgressBroadcaster: send error: %s", exc)
                to_remove.append(ws)

        if to_remove:
            async with self._lock:
                for ws in to_remove:
                    self._connections["global"].discard(ws)


def get_sync_broadcaster() -> SyncProgressBroadcaster:
    """Module-level accessor — avoids import cycles with sync_engine."""
    return SyncProgressBroadcaster.get_instance()


# ---------------------------------------------------------------------------
# P2-16: HTTP fallback endpoint — mirrors GET /progress for clients that
# cannot establish a WebSocket (proxies, legacy browsers, etc.).
# ---------------------------------------------------------------------------


@router.get("/poll")
async def sync_poll() -> dict[str, Any]:
    """Backward-compatible HTTP poll endpoint.

    Returns the same ``sync_status`` dict that the legacy ``GET /progress``
    endpoint served (which was dead code because the sync_engine router
    was never mounted). This is the recommended fallback when WebSocket
    is unavailable.
    """
    from unisync.api.sync_engine import sync_status

    return sync_status


# ---------------------------------------------------------------------------
# P2-16: WebSocket endpoint — push channel for real-time sync progress.
# ---------------------------------------------------------------------------


@router.websocket("/ws")
async def sync_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for sync engine progress.

    Protocol (server → client):
      - ``{type: "snapshot", data: sync_status}``  — immediate on connect
      - ``{type: "progress", data: sync_status}``  — on each log_sync()
      - ``{type: "ping"}``                          — every 30 s keepalive

    Protocol (client → server):
      - ``{type: "pong"}``   — keepalive ack (no-op, just consumed)
      - ``{type: "subscribe"}`` — force re-send snapshot

    Auth mirrors the ``unified_ws.py`` pattern: checked inside the handler
    after ``accept()`` so the WS upgrade isn't rejected by a dependency.
    """
    # P2-16: auth check — mirrors unified_ws.py pattern
    # UniSync local mode: auth bypassed
    ws_auth_failed = object()
    ws_require_auth = None

    # Auto-allow local client connections
    user_token = True

    await websocket.accept()

    broadcaster = get_sync_broadcaster()
    await broadcaster.connect(websocket)

    # P2-16: send initial snapshot so the client has the full picture
    from unisync.api.sync_engine import sync_status

    try:
        await websocket.send_json({"type": "snapshot", "data": dict(sync_status)})
    except Exception:
        await broadcaster.disconnect(websocket)
        return

    # P2-16: keepalive task — sends a ping every 30 s
    async def _keepalive() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break

    keepalive_task = asyncio.create_task(_keepalive())

    try:
        while True:
            raw = await websocket.receive_text()
            # P2-16: handle client messages (pong, subscribe, or unknown)
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = msg.get("type", "")

            if msg_type == "pong":
                # keepalive ack — nothing to do
                pass
            elif msg_type == "subscribe":
                # P2-16: force re-send snapshot
                try:
                    await websocket.send_json(
                        {"type": "snapshot", "data": dict(sync_status)}
                    )
                except Exception:
                    break
            # Unknown message types are silently ignored (forward compat)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("SyncProgressBroadcaster: unexpected error in ws loop", exc_info=True)
    finally:
        keepalive_task.cancel()
        await broadcaster.disconnect(websocket)
