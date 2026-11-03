# Echo360 Embed Sync (BLAW-style standalone videos) — Runbook

Courses like BLAW10001 publish no Echo360 section playlist. Their videos are
standalone media embeds scattered across Canvas pages. This pipeline surfaces
them in the lecture catalog. It has caused the most debugging pain; read this
before touching it.

## Data flow

1. Userscript `syncCanvas()` harvests Echo360 links from Canvas pages →
   POSTs `course.echo_embeds` via `/sync/payload`.
2. Backend persists them to `data/libraries/echo360_embeds.json` (keyed by
   Canvas course ID) — see `sync_engine.py` embed-save block.
3. `GET /lectures/libraries/echo360-sync-targets` serves `targets` (Echo360
   sections) + `embed_targets` (19 max recently; each carries `uuid`,
   `media_id`, `session_id`, `player_url`, `section_id`, `course_name`).
4. Userscript `syncEcho360()` resolves each embed (public page → LTI →
   subtitles → player-properties → poster), pushes `BLAW_EMBED_<uuid>`
   lessons into the matching section's `syllabus`, POSTs all sections to
   `/sync/payload`.
5. Backend overwrites `data/cache/echo360_syllabus.json` wholesale and queues
   downloads for items with media/subs.
6. `GET /lectures/catalog` merges syllabus + media library for the gallery.

## `auto_sync` scope (dashboard Courses tab → Course Sync card)

- A turned-off course is skipped in **every** sync: Echo360 sections, embeds,
  Canvas files, and assignments. The userscript crawl checks
  `GET /lectures/libraries` each run and skips disabled Canvas IDs with a
  console line.
- Re-enable a course with one toggle; effects apply on the next run
  (no restart for userscript/dashboard changes). Backend Python changes need
  a server restart (uvicorn runs WITHOUT `--reload`).

## Gotchas (each cost a debugging session)

1. **Synthetic IDs 404.** `BLAW_EMBED_<uuid>` is not a real lesson; the
   `/lesson/<id>/classroom` template 404s. Catalog maps embeds to
   `/media/<media_id>/public` (`lectures.py`, `_classroom_url`).
2. **Nested item shape.** Catalog reads `item.lesson.medias` for `has_media`.
   Embed items must nest as `{lesson: {lesson: {...}, medias: [...]}}` like
   real syllabus entries — a flat shape lists in pills but shows "No matching
   lectures" in the gallery (which filters `has_media`).
3. **Two gates.** Userscript always pushes embeds (visibility-only); backend
   only queues downloads when media/subs exist (`sync_engine.py`, 1121). A
   stream-less embed is a classroom link, never library junk.
4. **Wholesale overwrite.** Every push replaces the whole syllabus cache.
   Skipped (already-ingested) lessons must still be pushed as shells or they
   vanish from the catalog.
5. **Session IDs + LTI linkIds.** Standalone media uuids are NOT session IDs.
   Worse: one LTI tool id serves *different videos per `linkId`* (each Canvas
   page embeds its own link). Embed identity is `(tool uuid, linkId)`;
   lessons are `BLAW_EMBED_<uuid>_<link8>`. The resolver must fetch
   `/lti/<uuid>?linkId=<link>` — the bare tool URL resolves nothing. The
   public page HTML didn't contain sessionId matches. Never collapse links.
6. **No subtitles** for these embeds (`sub=false` everywhere). Nothing is
   cached; don't expect CC badges.
7. **Loopback.** Page-context `fetch()` to `127.0.0.1:8000` is blocked by
   Chrome (public→loopback). All script↔backend traffic must use
   `GM_xmlhttpRequest`. If the console goes totally silent + no heartbeats,
   suspect extension loopback permission, and check the Tampermonkey badge
   (2 scripts = duplicate install; duplicates double every log line).
8. **Silent returns kill.** Every early-return in the sync path must
   `console.warn` with the reason (v3.2.13+ pattern).
9. **Duplicate tabs** each run the full bridge; backend overwrites make this
   harmless but logs pair up.
10. **OOM segfaults.** docling PDF processing can `std::bad_alloc` and take
    the server down (exit 139). KB ingest now content-hash dedups identical
    files and records parse failures/empties in the manifest instead of
    retrying forever (`services/kb/service.py`).

## Rollback anchors

- v3.2.10 (pre-embed-visibility) both userscript copies: commit `0ee9e87`.
- Served script version: `GET /lectures/echo360/connector-version` (reads the
  backend asset from disk per request — userscript/dashboard.html changes need
  no restart; backend `.py` changes do).
- Test gate: `C:/Users/Admin/miniconda3/python.exe -m pytest -x -q` (204 tests).
