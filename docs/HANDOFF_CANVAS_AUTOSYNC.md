# Handoff: Canvas + Echo360 automatic course sync

## Status

**Implementation is present in the uncommitted worktree; the final browser acceptance test is incomplete.**

The latest FreeBuff session implemented the v2.34 connector and backend-owned
sync coordination.  The user then updated/reloaded the userscript and saw
**`Starting…` with a green checkmark**.  FreeBuff hit its usage limit before
that state could be inspected, so this is the first item to resume.

### Recovery update — 20 Aug 2026

The reported `[DEEPTURTOL_SYNC_NOW] … after 1800s` error was **not an Echo360
authentication diagnosis**.  The backend's Windows reload supervisor had
bound port 8001 while its child was unresponsive, so both the API and the web
proxy accepted connections without returning HTTP.  The generic client timer
then eventually attached the incorrect Echo360/LMS hint.

This is now addressed in **connector v2.38.0** (including the v2.36 lease
recovery changes):

- `run_server.py` uses one reliable backend process by default; hot reload is
  an explicit `DEEPTURTOL_BACKEND_RELOAD=1` opt-in.
- `Sync now` receives a quick durable-queue acknowledgement, while status
  polling owns the long-running import lifecycle.  It no longer relies on a
  single 30-minute postMessage response.
- The Library now reads that status directly from `GET /api/v1/sync/owner`.
  This avoids a false “backend is not running” error when the userscript is
  busy with the sync and cannot answer a short postMessage probe in time.
- A command remains retained after an owner claims it and is re-delivered if
  that tab releases or loses the lease; it is cleared only on a terminal
  heartbeat result.
- The lease protocol fences off an already-open pre-v2.38 tab.  That prevents
  an outdated worker from reintroducing the historical Echo360 `403` download
  path during a browser-assisted recovery sync.
- `Sync now` queues directly through the backend API. The authenticated owner
  still performs the sync, but a lost userscript postMessage acknowledgement
  can no longer turn a successfully queued command into a 20-second UI error.
- A resolved Canvas LTI launch is retried until its recording is actually
  present in the library.  This fixes BLAW's state where the signed Workshop 1
  recording had a captured media id but no successful local import.
- Autoplay-disabled LTI players are started muted in their background resolver
  tab so the official player requests a capturable media stream. Known embeds
  are attempted before the slower all-course Canvas refresh, and capture or
  download failures are persisted for the next retry instead of disappearing.
- Both services were restarted and checked: `/api/v1/sync/owner` responds and
  the served userscript reports `@version 2.38.0`.

Tampermonkey needs one update/reload to inject v2.38.0 into an open tab before
the changed client-side acknowledgement behavior is active.

This handoff was derived from the exported conversation at
`exports/freebuff-upgrade-connector-2026-08-20.md` and checked against the
current source tree.  Do not assume the old dev-server processes from that
session are still alive.

## Product goal

DeepTutor should automatically maintain one knowledge base per enrolled Canvas
subject, link each subject to its Echo360 course where one exists, and import
Canvas material plus Echo360 recordings in the background.  The library should
ultimately act like a Netflix-style course catalogue: show every available
recording, resume watched recordings, queue the next unwatched recording first,
then queue the rest chronologically.  Manual import-era controls should not be
required for normal operation; `Sync now` is the recovery/expedite control.

The user explicitly does **not** want to distribute or maintain a browser
extension.  The supported execution model is therefore a Tampermonkey
userscript that can piggyback on any open browser tab.  A userscript cannot run
when no matching browser tab exists; zero-tab background sync would require a
real extension.

## What is implemented

### Course libraries and subject matching

- Canvas course discovery auto-registers course libraries and the connector
  reports Canvas authentication state to the backend.
- Echo360 discovery has a same-origin tab fallback when its direct request is
  unauthenticated.  It deliberately uses the Canvas LMS launch route for SSO,
  not Echo360's standalone login page.
- The matcher normalizes base subject codes (for example,
  `CCDP10003_2026_SM2` and `CCDP10003`) and has a normalized-name fallback.
  It merges split Canvas-only/Echo360-only course libraries when a match is
  found.  The transcript reports this successfully merged the duplicate Video
  Games library and linked BLAW, COMP10001, INFO10003, and MPMP.
- Per-course KB creation was fixed after `_ensure_kb_exists()` silently passed
  unsupported keyword arguments to `update_kb_status()`.  The session reports
  that the existing libraries were backfilled and that `handbooks` remains the
  default KB.  Do not reintroduce the broad exception that hid that error.

### Content and media import

- Canvas snapshots include pages, modules, assignments, discussions,
  announcements, quizzes, and attachments.  The page cap was increased during
  the session.
- Echo360 media on `content.echo360.net.au` must be downloaded in the browser
  and uploaded to the backend, because the backend does not have the user's
  Echo360 cookies.  The automatic loop now follows that same browser-download
  branch as the old manual importer.
- HLS requests receive a cookie header collected through `GM_cookie`; the
  older MP4-only fix did not cover `.m3u8` sources.
- Download preference was changed to high-definition variants first, and the
  backend HLS downloader chooses the highest-bandwidth playlist variant.
- BLAW has Canvas-embedded Echo360 items that are not necessarily listed in a
  normal Echo360 syllabus.  The collector now scans module `ExternalTool`
  items and preserves previously resolved embed state across re-imports.
  Signed Canvas LTI launches still need browser navigation to resolve reliably.
  Direct/public Echo360 links are intentionally skipped after they produced
  expired-share 403s.

### Automatic sync ownership (v2.36+)

The prior implementation kept a leader lease in Tampermonkey storage.  It
could race across tabs, lost user-visible state on reload, and had a
background-tab heartbeat bug that opened duplicate worker tabs.  The current
architecture moves the authoritative lease to the backend:

```text
Any open tab with the userscript
        │ claim / heartbeat / progress
        ▼
FastAPI sync-owner manager
        │ atomic owner + command queue + progress snapshot
        ├── GET /api/v1/sync/owner         → UI/status polling
        └── WS  /api/v1/sync/ws            → immediate takeover notification
```

- `POST /owner/claim`, `/heartbeat`, and `/release` provide the atomic lease.
- `GET /owner` exposes owner id, stage, message, completed/total counts, last
  activity, and the most recent result.
- `POST /owner/command` queues `Sync now`; `POST /owner/command/poll` assigns
  it to an owner but retains it until the owner posts a terminal result.  This
  permits at-least-once re-delivery after a tab handoff without duplicate
  launches during the ordinary 500 ms poll.
- The connector adds `X-DT-Sync-Owner` to its backend calls.  Middleware uses
  that real traffic to record activity, while a watchdog evicts a stale owner
  and broadcasts `sync_takeover` over the WebSocket.
- The connector runs on `*://*/*`, uses `GM_xmlhttpRequest` for loopback
  backend calls, and can therefore operate from an unrelated open tab.  It no
  longer creates a permanent `?worker=1` tab.

The manager is deliberately in-memory and single-process.  A backend restart
clears the lease/progress snapshot, but committed imports are idempotent and
will be skipped/retried safely by the next sweep.  Do not run multiple backend
workers for this manager without moving its state to a shared store.

### Harrier embedding sidecar

`deeptutor/api/run_server.py` now starts the Harrier QNN embedding sidecar when
the active profile requires it.  It reuses the launcher command, avoids
starting a duplicate when port 8765 already answers, and registers child
shutdown.  This matters because a successful download otherwise fails only at
the final indexing stage when `127.0.0.1:8765` is absent.

### Responsive Canvas indexing

Canvas attachment uploads use a six-request browser pool, while document
parsing and embedding contain CPU-heavy sections that do not yield to asyncio.
Those background tasks previously ran on the FastAPI event loop and could make
`/sync/owner` time out while several copies of the indexing stack consumed
memory. `deeptutor/api/routers/lectures.py` now runs Canvas content and file
indexing on a serialized worker thread. Binary attachments remain on their
debounced background schedule and are no longer part of manual **Sync Now**, so
recording recovery is not delayed behind a large attachment course.

## Key files

| File | Why it matters |
| --- | --- |
| `deeptutor/api/routers/sync_owner.py` | New in-memory atomic lease, command queue, watchdog, and WebSocket implementation. |
| `deeptutor/api/main.py` | Starts/stops the manager, touches owner activity in middleware, and mounts `/api/v1/sync`. |
| `deeptutor/api/routers/lectures.py` | Course registration, subject matching, Canvas import, Echo360 import, KB routing, and embedded-media handling. |
| `deeptutor/api/run_server.py` | Starts the Harrier sidecar for standalone backend launches. |
| `deeptutor/assets/echo360_userscript/echo360-connector.user.js` | Canonical v2.38 userscript: browser-side imports, backend lease client, and sync loops. |
| `web/public/echo360-connector.user.js` | Served/synchronized userscript copy; keep byte-identical to the canonical asset. |
| `web/app/(workspace)/library/page.tsx` | Current library UI and `Sync now` relay/status presentation. |
| `web/lib/echo360-userscript.ts` and `web/lib/canvas-userscript.ts` | Web client/types around the connector. |

## Verification already performed

### Freshly rechecked during this handoff

- `node --check` passes for both userscript copies.
- The canonical and public userscript copies have the same SHA-256:
  `92C0517E8AD3A950C1B1B0C3AE280E139499BACA00047C9F2298055786E136E8`.
- `git diff --check` found no whitespace errors in the sync/connector files.
- Python regression tests for standalone launch and command redelivery pass
  (`4 passed`); the web node suite passes (`277 passed`).
- The current source contains the v2.38 metadata, backend lease endpoints,
  `X-DT-Sync-Owner` tagging, and `sync_takeover` handling.
- A full web TypeScript check still reports the unrelated pre-existing
  `PebbleConversationSwitcher.tsx` `compact_title` type error.  It was not
  changed as part of this work.

### Reported by the prior session (repeat if regression-sensitive)

- The final v2.34 implementation was tested through HTTP for
  claim/heartbeat/eviction/release/command and through the takeover WebSocket.
- Earlier checkpoints reported the lecture-router tests as green (39 tests at
  the module-embed/KB point) and JavaScript syntax checks as clean.
- These claims are useful history, not a substitute for a new browser session
  test after FreeBuff/server restarts.

## Immediate resume procedure

1. Start the backend and web UI using the normal project scripts.  Confirm the
   backend is serving this checkout and that the Harrier health endpoint works
   when the Harrier embedding profile is active.
2. Confirm Tampermonkey has **DeepTurtol Echo360 + Canvas Connector v2.38.0**;
   reload an ordinary open browser tab after updating, because an already-open
   tab keeps the old injected script.
3. Inspect `GET http://127.0.0.1:8001/api/v1/sync/owner` when the UI says
   `Starting…`.
   - No owner: the script is not injected, cannot contact the backend, or has
     not won the claim yet.
   - Owner with no activity/stage movement: inspect the corresponding
     browser-console/userscript errors and the backend request log.
   - Owner and a moving stage/count: the green checkmark is likely a normal
     availability/claim indication; let the status line show real progress.
4. Press `Sync now` once and verify that its command is accepted, drained once,
   and results in stages in this order: course registration, known
   embedded-media resolution, Echo360 import, Canvas snapshot/content import,
   and a second embedded-media resolution pass. Canvas file uploads run on
   their separate debounced schedule. Reload the library page during the run
   and verify the backend state still reports it. Close the owner tab only if
   testing takeover, then verify another open tab claims after the watchdog
   evicts it.
5. Use live Canvas/Echo360 authentication for this test.  Never put user
   cookies, API keys, or the secrets that appeared in the FreeBuff transcript
   into source, tests, logs, or future handoffs.

## Known gaps and planned work

1. **The Netflix library experience is not implemented.**  The library still
   primarily lists already-downloaded media.  It does not yet render the full
   Echo360 syllabus as available/downloaded cards, choose the next unwatched
   lecture, queue the remaining items chronologically, or generate midpoint
   thumbnails.  This was explicitly acknowledged in the transcript; do not
   describe it as a regression fix.
2. **Browser-level end-to-end acceptance is pending.**  The latest observed UI
   state was `Starting…` with a green checkmark.  Diagnose this before adding
   another architectural layer.
3. **BLAW needs a live validation after a refreshed Canvas snapshot.**  Module
   scanning should discover the LTI launches, but resolution/download still
   depends on the active Canvas/Echo360 browser session.
4. **The server lease is process-local.**  It is correct for the normal
   single-process development server, but it is not a multi-worker or durable
   coordination mechanism.
5. **Avoid touching unrelated dirty changes.**  This worktree contains broad,
   user-owned modifications across RAG, voice, parsing, and UI.  No commit was
   made for this sync effort; stage and review only the relevant files.

## Recommended next implementation order

1. Acceptance-test v2.38's BLAW-first manual sync in a freshly reloaded
   Tampermonkey tab.
2. Add an explicit, durable library-catalog model for every discovered Echo360
   recording, separate from the downloaded-media records.
3. Implement a checkpointed queue policy using existing watch progress:
   next unwatched first, then chronological remainder, with per-course limits
   and visible queued/downloading/failed/retry state.
4. Add server-generated thumbnails (an FFmpeg midpoint frame) and show them on
   library cards.
5. Add regression tests for command exactly-once behavior, owner eviction,
   cross-tab takeover, HLS cookie forwarding, Canvas-module LTI discovery, and
   subject-code merge/KB registration.
