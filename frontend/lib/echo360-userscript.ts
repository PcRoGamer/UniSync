/**
 * @deprecated [DEPRECATED] Replaced by CamoufoxService (backend/unisync/services/browser/camoufox_service.py).
 * Narrow WebUI boundary for the legacy Tampermonkey Echo360 connector.
 *
 * The browser userscript executes requests in an already-signed-in Echo360
 * page. This module receives only its non-secret results; it never has access
 * to browser cookies or the Echo360 iframe's DOM.
 */
const WEB_SOURCE = "unisync-web";
const CONNECTOR_SOURCE = "unisync-echo360-userscript";

type ConnectorReply<T> = {
  source: typeof CONNECTOR_SOURCE;
  requestId: string;
  ok: boolean;
  result?: T;
  error?: string;
};

/** Streaming progress emitted by the connector mid-request (best effort).
 *  ``total`` of 0 means the connector doesn't know the total yet — treat it
 *  as indeterminate. */
export type ConnectorProgress = {
  phase: string;
  message: string;
  done: number;
  total: number;
};

export type Echo360Course = {
  id: string;
  name: string;
  url: string;
  /** Subject code (``COMP10001``) when Echo360 reports one; used to link to the matching Canvas course. */
  code?: string;
};
export type Echo360Recording = {
  id: string;
  title: string;
  date: string;
  course_id: string;
  course_name: string;
  has_media: boolean;
};
export type Echo360Source = {
  id: string;
  title: string;
  date: string;
  course_id: string;
  course_name: string;
  media_url: string;
  media_kind: "mp4" | "mp3" | "hls";
  /** Browser-resolved Echo360 media id (needed by the captions API). */
  media_id?: string;
  /** Official captions (WebVTT) fetched inside the authenticated browser. */
  transcript_vtt?: string;
  /** Non-HttpOnly Echo360 session cookies, used for media downloads. */
  cookie_header?: string;
};

/** Minimal metadata for a recording, sent alongside SOURCES requests so the
 *  connector can include titles/dates in its response even when the Echo360
 *  syllabus API is unreachable. */
export type Echo360RecordingMeta = {
  [id: string]: { title: string; date: string };
};

export function connectorRequest<T>(
  type: string,
  payload?: Record<string, unknown>,
  timeoutMs = 20_000,
  onProgress?: (progress: ConnectorProgress) => void,
): Promise<T> {
  if (typeof window === "undefined") {
    return Promise.reject(new Error("The Echo360 connector is available only in the WebUI."));
  }
  const requestId = crypto.randomUUID();
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMessage);
      const hint = type === "UNISYNC_SYNC_NOW"
        ? "Make sure the UniSync backend and connector are running, then try again."
        : type.startsWith("UNISYNC_CANVAS_")
          ? "Open canvas.lms.unimelb.edu.au in this browser and sign in, then try again."
          : "Open Echo360 through UniMelb LMS, then try again.";
      reject(new Error(
        `[${type}] The connector did not respond after ${(timeoutMs / 1000).toFixed(0)}s. ${hint}`,
      ));
    }, timeoutMs);
    function onMessage(event: MessageEvent<ConnectorReply<T> & { type?: string; progress?: ConnectorProgress }>) {
      if (
        event.origin !== window.location.origin ||
        event.data?.source !== CONNECTOR_SOURCE ||
        event.data.requestId !== requestId
      ) {
        return;
      }
      // Streaming progress is advisory — keep the request alive and pass it up.
      if (event.data.type === "progress") {
        onProgress?.(event.data.progress as ConnectorProgress);
        return;
      }
      window.clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      if (event.data.ok && event.data.result) resolve(event.data.result);
      else reject(new Error(event.data.error || "The Echo360 connector could not complete that request."));
    }
    window.addEventListener("message", onMessage);
    window.postMessage({ source: WEB_SOURCE, requestId, type, payload }, window.location.origin);
  });
}

export type ConnectorInfo = { installed: boolean; version?: string };

export async function hasEcho360Connector(): Promise<boolean> {
  try {
    const result = await connectorRequest<ConnectorInfo>(
      "UNISYNC_ECHO360_PING",
      undefined,
      2_500,
    );
    return result.installed === true;
  } catch {
    return false;
  }
}

export async function getConnectorInfo(): Promise<ConnectorInfo | null> {
  try {
    return await connectorRequest<ConnectorInfo>(
      "UNISYNC_ECHO360_PING",
      undefined,
      2_500,
    );
  } catch {
    return null;
  }
}

export async function getExpectedConnectorVersion(): Promise<string | null> {
  try {
    const res = await fetch("/echo360-connector-version");
    if (!res.ok) return null;
    const data = await res.json();
    return typeof data.version === "string" ? data.version : null;
  } catch {
    return null;
  }
}

export function openEcho360ThroughLms(): Promise<{ message: string }> {
  return connectorRequest("UNISYNC_ECHO360_OPEN_LMS");
}

export function closeLmsTab(): Promise<{ closed: boolean }> {
  return connectorRequest("UNISYNC_ECHO360_CLOSE_LMS", undefined, 5_000);
}

export function checkLoginTabActive(): Promise<{ active: boolean; url?: string }> {
  return connectorRequest<{ active: boolean; url?: string }>("UNISYNC_CHECK_LOGIN_TAB", undefined, 3_000);
}

export type Echo360AuthCheckResult = {
  authenticated: boolean;
  courses?: Echo360Course[];
  reason?: string;
};

export function checkEcho360Auth(): Promise<Echo360AuthCheckResult> {
  return connectorRequest<Echo360AuthCheckResult>("UNISYNC_ECHO360_CHECK_AUTH", undefined, 10_000);
}

export type WaitForAuthOptions = {
  /** Maximum duration to wait before timing out (defaults to 10 minutes / 600,000 ms). */
  timeoutMs?: number;
  /** Interval between authentication probes in milliseconds (defaults to 2,000 ms). */
  pollIntervalMs?: number;
  /** Optional callback invoked on every poll iteration with elapsed, remaining ms, and active tab presence. */
  onProgress?: (elapsedMs: number, remainingMs: number, isTabActive?: boolean) => void;
  /** Optional abort signal to cancel polling when the user dismisses the waiting UI. */
  signal?: AbortSignal;
};

export async function waitForEcho360Auth(
  options: WaitForAuthOptions = {},
): Promise<Echo360Course[]> {
  const {
    timeoutMs = 600_000,
    pollIntervalMs = 2_000,
    onProgress,
    signal,
  } = options;

  const startTime = Date.now();
  while (Date.now() - startTime < timeoutMs) {
    if (signal?.aborted) {
      throw new Error("Authentication wait cancelled.");
    }

    try {
      const auth = await checkEcho360Auth();
      if (auth.authenticated && Array.isArray(auth.courses) && auth.courses.length >= 0) {
        return auth.courses;
      }
    } catch {
      try {
        const body = await listEcho360Courses();
        if (body?.courses) {
          return body.courses;
        }
      } catch {
        // Still unauthenticated
      }
    }

    let isTabActive = false;
    try {
      const tabStatus = await checkLoginTabActive();
      isTabActive = !!tabStatus.active;
    } catch {
      // Best-effort
    }

    const elapsed = Date.now() - startTime;
    const remaining = Math.max(0, timeoutMs - elapsed);
    onProgress?.(elapsed, remaining, isTabActive);

    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }

  throw new Error(
    "Authentication timed out. Make sure you completed the UniMelb sign-in in the open Canvas tab, then try again.",
  );
}

export function listEcho360Courses(): Promise<{ courses: Echo360Course[] }> {
  return connectorRequest("UNISYNC_ECHO360_COURSES", undefined, 60_000);
}

export function listEcho360Recordings(
  courseId: string,
): Promise<{ courseName: string; recordings: Echo360Recording[] }> {
  return connectorRequest("UNISYNC_ECHO360_RECORDINGS", { courseId }, 60_000);
}

export type SyncNowResult = {
  synced: boolean;
  /** True once the backend has durably queued the work for the owner tab. */
  queued?: boolean;
  alreadyRunning?: boolean;
  message?: string;
};

/**
 * Trigger a full on-demand sync in the connector: course discovery,
 * Echo360 recording import, Canvas content import, embedded-media resolution
 * and attachment download.  The connector answers with streaming progress
 * and a fast queue acknowledgement; normal sync-status polling owns the
 * long-running progress/completion lifecycle instead of one fragile browser
 * postMessage request.
 */
export function syncNow(
  onProgress?: (progress: ConnectorProgress) => void,
): Promise<SyncNowResult> {
  // Queue acknowledgement is local and should not wait for the entire import.
  return connectorRequest<SyncNowResult>(
    "UNISYNC_SYNC_NOW",
    undefined,
    20_000,
    onProgress,
  );
}

export type SyncStatusResult = {
  syncing: boolean;
  message?: string;
};

/**
 * Read the current sync state from whichever tab owns the sync pipeline (via
 * the connector).  Used to resume showing an in-flight sync after the library
 * page is reloaded or closed and reopened — the owner keeps running either way.
 */
export function syncStatus(): Promise<SyncStatusResult> {
  return connectorRequest<SyncStatusResult>("UNISYNC_SYNC_STATUS", undefined, 5_000);
}

export function resolveEcho360Sources(
  courseId: string,
  recordingIds: string[],
  recordings?: Echo360Recording[],
): Promise<{ sources: Echo360Source[]; uploaded?: any[] }> {
  // Build metadata lookup for recordings selected for import
  let recordingsMeta: Echo360RecordingMeta = {};
  if (recordings) {
    for (const r of recordings) {
      if (recordingIds.includes(r.id)) {
        recordingsMeta[r.id] = { title: r.title, date: r.date };
      }
    }
  }
  // The Echo360 tab navigates through each recording's classroom page at
  // ~36s per recording (30 attempts × 1200ms).  Add a generous buffer.
  const perRecordingMs = 60_000;
  const timeoutMs = Math.max(120_000, recordingIds.length * perRecordingMs);
  return connectorRequest(
    "UNISYNC_ECHO360_SOURCES",
    { courseId, recordingIds, recordings: recordingsMeta },
    timeoutMs,
  );
}
