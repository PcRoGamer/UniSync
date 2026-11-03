/**
 * @deprecated [DEPRECATED] Replaced by CamoufoxService (backend/unisync/services/browser/camoufox_service.py).
 * Narrow WebUI boundary for the legacy Tampermonkey connector's Canvas content mode.
 *
 * The browser userscript pulls course content from the user's already-signed-in
 * UniMelb Canvas session (same cookies/CSRF the Canvas frontend uses — no API
 * token is ever created).  This module receives only its non-secret results;
 * it never has access to browser cookies or to the Canvas tab's DOM.
 *
 * Request flow mirrors the Echo360 connector: the library page asks, the
 * userscript answers via postMessage (direct cookie fetch, falling back to a
 * same-origin Canvas tab).
 */
import { connectorRequest, type ConnectorProgress } from "./echo360-userscript";

/** Streaming progress the connector reports mid-request (see ConnectorProgress). */
export type CanvasProgress = ConnectorProgress;

export type CanvasCourse = {
  id: string;
  name: string;
  code?: string;
  term?: string;
  url: string;
  image_download_url?: string;
};

export type CanvasUser = { id: string; name: string };

export type CanvasAuthCheckResult = {
  authenticated: boolean;
  user?: CanvasUser;
  courses?: CanvasCourse[];
  reason?: string;
};

/** One full Canvas course-content snapshot as collected in the browser.
 *  Every section is optional — the connector degrades gracefully per
 *  endpoint, so a course with no quizzes simply has an empty list. */
export type CanvasCourseContent = {
  course: Record<string, unknown>;
  modules: Record<string, unknown>[];
  pages: Record<string, unknown>[];
  files: Record<string, unknown>[];
  folders: Record<string, unknown>[];
  assignments: Record<string, unknown>[];
  discussions: Record<string, unknown>[];
  announcements: Record<string, unknown>[];
  quizzes: Record<string, unknown>[];
  outcomes: Record<string, unknown>[];
  front_page: Record<string, unknown> | null;
  fetched_at: string;
};

export type CanvasContentResult = {
  course: Record<string, unknown>;
  content: CanvasCourseContent;
};

/**
 * Check whether the browser has an active, authenticated Canvas session.
 *
 * Returns `{ authenticated: false }` (instead of rejecting) while the user is
 * still completing UniMelb SSO / Okta MFA in the tab the connector opens, so
 * the WebUI can poll non-disruptively.  When authenticated, the user's active
 * student enrollments are included as a convenience.
 */
export function checkCanvasAuth(): Promise<CanvasAuthCheckResult> {
  // Long timeout: the connector may need to open a Canvas tab and wait for
  // the user to finish signing in before the probe can succeed.
  return connectorRequest<CanvasAuthCheckResult>(
    "UNISYNC_CANVAS_CHECK_AUTH",
    undefined,
    150_000,
  );
}

export function listCanvasCourses(): Promise<{ courses: CanvasCourse[] }> {
  return connectorRequest("UNISYNC_CANVAS_COURSES", undefined, 150_000);
}

/**
 * Pull a full course-content snapshot (syllabus, pages, files, assignments,
 * discussions, announcements, quizzes + questions, modules, outcomes) from the
 * logged-in browser session.
 */
export function fetchCanvasCourseContent(
  courseId: string,
  onProgress?: (progress: CanvasProgress) => void,
): Promise<CanvasContentResult> {
  // Generous timeout: a large course with many pages/quizzes can take a while
  // to collect, especially through the Canvas-tab fallback.
  return connectorRequest<CanvasContentResult>(
    "UNISYNC_CANVAS_FETCH_CONTENT",
    { courseId },
    300_000,
    onProgress,
  );
}

export type CanvasFileUploadResult = {
  canvas_file_id: string;
  display_name: string;
  size: number;
  uploaded?: boolean;
  skipped?: boolean;
  task_id?: string;
  reason?: string;
};

/** Per-source scan diagnostics returned alongside the candidate download
 *  result.  Lets the library page explain an empty candidate pool
 *  (`"modules=19 had no File-type items"`) rather than just showing
 *  `0 candidates`. */
export type CanvasFilesSources = {
  files_listed: number;
  pages_with_hrefs: number;
  front_page_with_hrefs: boolean;
  modules: { file_items: number; page_items_with_hrefs: number };
  assignments: { with_hrefs: number; attachments_listed: number };
  discussions_with_hrefs: number;
  announcements_with_hrefs: number;
  quizzes_with_hrefs: number;
  failed_lookups: number;
};

export type CanvasFilesFetchResult = {
  total: number;
  uploaded_count: number;
  failed_count: number;
  uploaded: CanvasFileUploadResult[];
  failed: { canvas_file_id: string; display_name: string; reason: string }[];
  sources?: CanvasFilesSources;
};

/**
 * Download the Canvas course's attachments (PDFs, slides, docs) inside the
 * user's authenticated browser and queue each one into the linked knowledge
 * base.  File metadata + the signed download URL come from the Canvas API;
 * the bytes are streamed back to the backend via ``/canvas/upload`` so
 * binaries never have to fit through the JSON-only postMessage channel.
 *
 * The connector skips files the backend manifest already knows about
 * (``uploaded_file_ids``) so re-runs only fetch what's missing.
 *
 * This request deliberately bypasses the Canvas-tab fallback: it needs the
 * library-page ``GM_xmlhttpRequest`` + FormData combination to upload blobs
 * straight to the backend.  A missing direct session surfaces as a clean
 * auth error.
 */
export function fetchCanvasCourseFiles(
  courseId: string,
  options: {
    kbName?: string;
    courseName?: string;
    timeoutMs?: number;
    onProgress?: (progress: CanvasProgress) => void;
  } = {},
): Promise<CanvasFilesFetchResult> {
  // Even a modest course can hold hundreds of attachments; large PDFs plus
  // the upload round-trip easily push past the default 5-minute cap.
  const timeoutMs = options.timeoutMs ?? 30 * 60 * 1000;
  return connectorRequest<CanvasFilesFetchResult>(
    "UNISYNC_CANVAS_FETCH_FILES",
    {
      courseId,
      kbName: options.kbName ?? "",
      courseName: options.courseName ?? "",
    },
    timeoutMs,
    options.onProgress,
  );
}

/** A harvested Echo360 embed: the static LTI launch URL extracted from a
 *  Canvas page body.  ``canvas_launch_url`` is what the connector must open to
 *  perform the signed launch. */
export type CanvasEmbed = {
  uuid: string;
  title?: string;
  source_title?: string;
  source_kind?: string;
  canvas_launch_url: string;
  provider_launch_url?: string;
};

/** One embed after the connector resolved its LTI launch in the browser.
 *  ``media_url`` is the playable manifest the player fetched (short-lived) and
 *  is present only when the launch was resolved successfully. */
export type ResolvedCanvasEmbed = {
  uuid: string;
  title?: string;
  media_id: string;
  player_url: string;
  secure_link_access_data_id: string;
  media_url?: string;
  media_kind?: "mp4" | "mp3" | "hls";
  cookie_header?: string;
  transcript_vtt?: string;
  error?: string;
};

export type CanvasEmbedDownload = {
  uuid: string;
  title?: string;
  item_id?: string;
  error?: string;
};

export type CanvasEmbedsDownloads = {
  uploaded: CanvasEmbedDownload[];
  queued: CanvasEmbedDownload[];
  failed: CanvasEmbedDownload[];
  skipped: number;
};

export type CanvasEmbedsResolveResult = {
  resolved: ResolvedCanvasEmbed[];
  downloads?: CanvasEmbedsDownloads | null;
};

export type ResolveCanvasEmbedsOptions = {
  /** Also download each resolved media into the KB (needs kbName). */
  download?: boolean;
  kbName?: string;
  courseId?: string;
  courseName?: string;
};

/**
 * Resolve Echo360 LTI embeds to their runtime player/media ids, and optionally
 * download the media into the linked knowledge base.
 *
 * The connector opens a Canvas tab, walks it through each ``canvas_launch_url``
 * (which performs the signed LTI launch and lands on
 * ``echo360.net.au/ui/player/<mediaId>``), captures the media id + playable
 * URL, then downloads each (browser upload for mp4/mp3, backend HLS pipeline
 * for m3u8).  Needs active Canvas *and* Echo360 sessions.
 */
export function resolveCanvasEmbeds(
  embeds: CanvasEmbed[],
  options: ResolveCanvasEmbedsOptions = {},
  onProgress?: (progress: CanvasProgress) => void,
): Promise<CanvasEmbedsResolveResult> {
  // Each launch walks Canvas → Echo360 player; budget generously per embed.
  const timeoutMs = Math.max(120_000, embeds.length * 90_000);
  return connectorRequest<CanvasEmbedsResolveResult>(
    "UNISYNC_CANVAS_RESOLVE_EMBEDS",
    {
      embeds,
      download: options.download === true,
      kbName: options.kbName ?? "",
      courseId: options.courseId ?? "",
      courseName: options.courseName ?? "",
    },
    timeoutMs,
    onProgress,
  );
}