"use client";

/**
 * P2-16: WebSocket-backed hook for real-time sync-engine progress.
 *
 * Connects to `GET /api/v1/sync/ws` on mount and keeps the React state
 * in sync with the server's `SyncProgressBroadcaster` stream. Falls back
 * to HTTP polling via `GET /api/v1/sync/poll` every 3 s when:
 *
 *   1. The WS connection fails or is refused (e.g. proxy strips the upgrade),
 *   2. `NEXT_PUBLIC_USE_WS_PROGRESS` is explicitly set to `"false"`.
 *
 * Auto-reconnect uses exponential back-off (1 s → 2 s → 4 s … capped at 30 s)
 * mirroring the `unified-ws.ts` pattern.
 *
 * Consumers: `LectureCaptureControl`, `library/page.tsx`, sidebar widgets.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiUrl, wsUrl } from "@/lib/api";

// ── Public types ──────────────────────────────────────────────────────

export interface SyncProgressData {
  /** Whether the sync engine is currently processing. */
  is_syncing: boolean;
  /** Current progress counter and human-readable message. */
  progress: { done: number; total: number; message: string };
  /** Rolling log of the last ~100 sync messages. */
  log: string[];
}

// ── Constants ─────────────────────────────────────────────────────────

/** Base delay for exponential back-off (doubles each attempt). */
const BASE_BACKOFF_MS = 1000;
/** Maximum reconnect delay cap. */
const MAX_BACKOFF_MS = 30_000;
/** HTTP polling interval when WS is unavailable (ms). */
const POLL_INTERVAL_MS = 3000;

/** Default "idle" state before any snapshot arrives. */
const IDLE_STATE: SyncProgressData = {
  is_syncing: false,
  progress: { done: 0, total: 0, message: "" },
  log: [],
};

// ── Hook ──────────────────────────────────────────────────────────────

export function useSyncProgress(): SyncProgressData {
  const [state, setState] = useState<SyncProgressData>(IDLE_STATE);

  // Refs ---------------------------------------------------------------
  /** Live WS instance — not state-driven so we avoid stale-closure races. */
  const wsRef = useRef<WebSocket | null>(null);
  /** Monotonic reconnect delay (resets to 0 on successful open). */
  const backoffRef = useRef(0);
  /** Handle for the reconnect setTimeout so we can cancel on unmount. */
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  /** Handle for the HTTP-poll fallback setInterval (browser returns number). */
  const pollTimerRef = useRef<number | null>(null);
  /** Guard against state updates after unmount. */
  const mountedRef = useRef(true);
  /** Whether the WS path is available (opt-out via env flag). */
  const wsEnabled =
    process.env.NEXT_PUBLIC_USE_WS_PROGRESS !== "false";

  // ── Helpers ───────────────────────────────────────────────────────────

  /** Clear any pending reconnect timer. */
  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  /** Clear the HTTP-poll fallback interval. */
  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  /** Close the current WS connection (idempotent). */
  const closeWs = useCallback(() => {
    const ws = wsRef.current;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
      wsRef.current = null;
    }
  }, []);

  // ── HTTP polling fallback ──────────────────────────────────────────────

  /** Start polling `GET /api/v1/sync/poll` every 3 s. */
  const startPolling = useCallback(() => {
    clearPollTimer();
    pollTimerRef.current = window.setInterval(async () => {
      if (!mountedRef.current) return;
      try {
        const res = await fetch(apiUrl("/api/v1/sync/poll"));
        if (!res.ok) return;
        const data: SyncProgressData = await res.json();
        if (mountedRef.current) {
          setState({
            is_syncing: data.is_syncing,
            progress: data.progress,
            log: data.log ?? [],
          });
        }
      } catch {
        // Network blip — keep the last known state, retry next tick.
      }
    }, POLL_INTERVAL_MS);
  }, [clearPollTimer]);

  // ── WebSocket connection ───────────────────────────────────────────────

  const connectWs = useCallback(() => {
    if (!wsEnabled) {
      startPolling();
      return;
    }

    closeWs();
    clearReconnectTimer();

    const url = wsUrl("/api/v1/sync/ws");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      // Successful connection — reset back-off.
      backoffRef.current = 0;
      // Kill any active polling fallback since WS is live.
      clearPollTimer();
    };

    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(event.data) as {
          type?: string;
          data?: SyncProgressData;
        };

        // Server heartbeat → reply pong to keep the connection alive.
        if (msg.type === "ping") {
          ws.send(JSON.stringify({ type: "pong" }));
          return;
        }

        // snapshot / progress / complete all carry the same shape.
        if (
          (msg.type === "snapshot" ||
            msg.type === "progress" ||
            msg.type === "complete") &&
          msg.data
        ) {
          setState({
            is_syncing: msg.data.is_syncing,
            progress: msg.data.progress,
            log: msg.data.log ?? [],
          });
        }
      } catch {
        // Malformed frame — ignore, next message may be fine.
      }
    };

    ws.onerror = () => {
      // `onclose` fires immediately after; reconnect logic lives there.
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (!mountedRef.current) return;

      // Fall back to HTTP polling immediately so the UI stays fresh while
      // the WS reconnect loop retries in the background.
      startPolling();

      // Exponential back-off: 1 s → 2 s → 4 s → 8 s … capped at 30 s.
      const delay = Math.min(
        BASE_BACKOFF_MS * Math.pow(2, backoffRef.current),
        MAX_BACKOFF_MS,
      );
      backoffRef.current += 1;

      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (mountedRef.current) {
          connectWs();
        }
      }, delay);
    };
  }, [
    wsEnabled,
    startPolling,
    closeWs,
    clearReconnectTimer,
    clearPollTimer,
  ]);

  // ── Lifecycle ─────────────────────────────────────────────────────────

  useEffect(() => {
    mountedRef.current = true;
    connectWs();

    return () => {
      mountedRef.current = false;
      closeWs();
      clearReconnectTimer();
      clearPollTimer();
    };
  }, [connectWs, closeWs, clearReconnectTimer, clearPollTimer]);

  // ── Public API ────────────────────────────────────────────────────────

  return state;
}
