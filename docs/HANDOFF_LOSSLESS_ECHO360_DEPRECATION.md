# Agent Handoff: Lossless Camoufox Migration for Echo360 Transcript Ingestion

**Date:** 2026-10-30  
**Task:** Complete the Echo360/Canvas transcript-ingestion migration to Camoufox, preserving the legacy path's behavior and data before deprecating it.

## Objective

Make UniSync discover and ingest the student's current Canvas/Echo360 materials through the persistent Camoufox session, including BLAW weekly videos, lecture recordings, and seminars. Make the old browser bridge safely removable only after its user-visible behavior and stored data have a verified replacement. "Lossless deprecation" means no discovery, transcript, media, thumbnail, catalog, search/index, or session-recovery capability silently disappears during migration.

## Current measured state

These are observations from the live UniSync status and local cache on 2026-10-30; re-check before acting because another process may update them.

- Live sync status: `is_syncing=false`, `bridge_active=false`, `state=waiting_for_tab`, no active tab. The service reports latest lecture date `2026-09-24` and 28 later lecture dates pending, from `2026-09-29` through `2026-10-22` across enrolled courses.
- Camoufox is installed and has a persistent profile, but the status snapshot showed no resident browser, no in-memory cookies, and no verified authentication time. Do not infer that a saved profile means the current session is authenticated.
- `data/libraries/echo360_embeds.json`, BLAW Canvas course ID `240718`: 82 Canvas embed records. Matching by the link-specific identity (placement UUID plus `link_id`) found 80 cue-valid VTTs and 2 missing transcripts:
  - `Exam information and assistance in SWOT VAC`
  - `Seminar 4 Recording and Slides`
- The currently discovered numbered topic videos run from `1.1` through `8.8`; the exact link-specific cache check found transcripts for these numbered items. This is coverage of discovered links, not proof that Canvas contains no undiscovered video.
- The two missing links currently have no resolved `media_url` either. The embed catalog was modified on Oct 30, while the newest BLAW subtitle file was last modified Sep 24 at 17:15 Sydney time. Catalog freshness and transcript freshness are separate signals.
- There are 97 BLAW-prefixed VTT files in the subtitle cache, but that raw count is not a coverage measure: the cache includes stale/orphan files and generic IDs.
- `data/cache/echo360_syllabus.json` is a separate older snapshot (93 BLAW-prefixed rows, dated Sep 27); do not treat its row count or generic lesson IDs as proof of current Canvas link coverage.

**Correction to earlier diagnosis:** A prior check incorrectly treated a VTT named only with the shared Echo360/LTI UUID as satisfying every Canvas embed. That is a false positive when a link ID exists. `backend/unisync/api/lectures.py` explicitly warns against falling back to `BLAW_EMBED_<uuid>.vtt` for link-specific embeds. The correct identity must retain the Canvas course and link ID.

## Code map and migration gap

- `backend/unisync/assets/echo360_userscript/bridge-runtime.js`
  - `harvestBlaw()` enumerates BLAW Canvas pages and module items, extracts embedded Echo360 links, and sends discovery payloads.
  - Later bridge handlers intercept player transcript requests / fetch VTT or JSON cue sources and POST them to `/api/v1/lectures/subtitles/ingest`.
  - Treat this as the legacy behavioral reference. Inventory the rest of its behaviors before removal (media archiving, thumbnails, status/heartbeat, retries, and identity handling included).
- `backend/unisync/api/dashboard.py`
  - Userscript and bridge-runtime routes are explicitly deprecated and advertise Camoufox as the replacement.
- `backend/unisync/services/browser/camoufox_service.py`
  - Provides persistent profile/session handling and one-off `resolve_echo360_embed()` browser navigation. That resolver currently captures selected request URLs; it does not itself ingest transcript bodies or crawl all course pages.
- `backend/unisync/services/sync/canvas_client.py`
  - Native authenticated Canvas API client can fetch courses, pages, modules, assignments, and tabs using the Canvas session transport.
- `backend/unisync/api/sync_engine.py`
  - `_sync_native_canvas_course()` extracts Echo360 links from Canvas pages/assignments/module items and persists them via `_persist_canvas_echo_embeds()`.
  - `_run_sync_pipeline()` fetches Echo360 sections and syllabi but currently only logs the returned syllabus counts; it does not route those results through the legacy subtitle/cache ingestion path.
  - The `sync_payload` `echo360_sections` branch contains much of the older subtitle/media processing behavior. Reuse/refactor that behavior into a native service; do not keep a deprecated browser bridge as an invisible runtime dependency.
- `backend/unisync/api/lectures.py`
  - Owns catalog presentation, subtitle storage, identity matching, and the `/subtitles/ingest` endpoint. Align all producers and readers on one canonical per-link key.

## Required implementation

1. **Build a full feature inventory from the legacy bridge.** Record every user-visible behavior and its replacement location. At minimum cover paginated Canvas page/module discovery, nested/encoded LTI URLs, link IDs, Echo360 resolution, VTT and JSON subtitles, HLS caption tracks, media ingestion, thumbnail capture, status/progress, retries/resume, and indexing/search refresh.
2. **Make native discovery complete and incremental.** Use authenticated Camoufox-backed Canvas API access; paginate all pages/modules/items; retain source course, page/module title, Canvas URL, provider URL, UUID, `link_id`, actual media ID, and timestamps. Deduplicate by a canonical `(canvas_course_id, uuid, link_id)` key, not the generic tool UUID alone.
3. **Resolve and ingest transcripts in the Camoufox path.** Capture transcript response bodies or fetch accessible caption/transcript endpoints in the authenticated browser context. Support VTT, Echo360 JSON cue arrays, and HLS subtitle renditions already handled by the legacy bridge. Validate `WEBVTT` plus at least one timestamped cue before marking a transcript present. Write files atomically and idempotently. A generic UUID file must never satisfy a link-specific record.
4. **Preserve media and thumbnail behavior.** Keep the existing direct browser-to-disk media ingest and thumbnail cache path working, with stable association to the canonical embed key. A concurrency throttle may limit simultaneous requests, but must queue/resume the whole discovered corpus rather than skip or hide materials.
5. **Close the current course-wide gap.** Ensure the native full sync actually persists and processes each fetched Echo360 syllabus instead of merely logging its size. Merge discovered Canvas embeds with the Echo360 section syllabus without overwriting richer metadata or losing legacy records.
6. **Make auth/session recovery coherent.** Use the persistent Camoufox profile and the existing interactive-login flow when auth expires. A background sync should report `login_required` and open the interactive login as configured; it must not report success on an unknown/unauthenticated session.
7. **Make updates automatic and resumable.** Run a native incremental discovery/transcript sync after a verified session becomes available, at app startup, and on a configurable periodic schedule; keep manual full and per-course refresh controls. Do not require a user to leave Canvas open. If auth expires, open the configured interactive login, then resume pending work after authentication. Persist the schedule/run state so a restart does not lose the queue.
8. **Expose honest coverage and freshness.** Report per course: last Canvas discovery, last Echo360 syllabus fetch, discovered link count, resolved media count, cue-valid transcript count, missing count/reasons, and last successful transcript update. Distinguish "catalog refreshed" from "transcripts current". Make pending items actionable and resumable.
9. **Deprecate in phases, without data loss.** Keep the legacy routes/runtime and readable existing cache during the transition. Migrate all consumers, retain backward-compatible identifiers/metadata, and only remove/decommission the old path after the native path demonstrates parity for the inventory in item 1. Preserve the current local catalog, subtitle cache, media library, and unrelated dirty-worktree changes; do not use destructive Git reset/restore/clean commands.

## Completion criteria

- The current BLAW catalog is reconciled link-by-link; every discovered link is classified as cue-valid transcript, confirmed no transcript source, or actionable resolution/auth failure. No generic-ID false positives.
- The two currently missing links are retried through the native path and their outcomes are recorded accurately. Do not claim a transcript if the source has none or cannot be fetched.
- A native full sync can discover additions and changes from Canvas pages/modules, process Echo360 section syllabi, save valid transcripts, and resume failures without a manually open browser tab or userscript. Startup and configured periodic triggers run it after authentication; manual full and per-course refreshes remain available.
- The sync status remains honest when authentication is unknown, the browser is idle, or items are pending; dates and counts are backed by current disk/catalog state.
- The legacy path remains available as a rollback/compatibility path until its feature inventory has a native replacement and existing on-disk materials remain readable.
- Summarize the parity mapping, actual coverage after the change, remaining inaccessible items, and any behavior intentionally retired with evidence.

## Working-tree caution

The UniSync worktree already contains numerous modifications and untracked files from ongoing session-survival, native Canvas ingestion, and submission-sync work. Inspect `git status --short` before edits. Preserve unrelated changes and local data; do not reset, restore, or clean the worktree.


