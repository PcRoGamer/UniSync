# Echo360 Native Ingestion — Legacy Feature Inventory & Phased Deprecation

**Status:** native path implemented and test-verified (43 tests green under the UniSync interpreter); legacy bridge retained as rollback (not yet removed). A notice-only (disclaimer) track guard is now in place across readers and writers — see §3.3.
**Scope:** Echo360/Canvas transcript, media, thumbnail, catalog, discovery and session recovery.
**Canonical identity:** `(canvas_course_id, uuid, link_id)`.
**Interpreter:** run backend code with `C:/Users/Admin/miniconda3/python.exe` (`PYTHONPATH=backend`), exactly as `test_launcher.bat` does. The repo `.venv` has no camoufox, so tests that import it fail there.

The legacy userscript `backend/unisync/assets/echo360_userscript/bridge-runtime.js` requires a real
Canvas/Echo360 tab, polls the backend every 15s, and stores discovery state in user-script storage.
The native path performs the same work inside the persistent Camoufox profile, so no tab is needed.

## 1. Feature inventory → native replacement

| Legacy behavior | Legacy location (`bridge-runtime.js`) | Native replacement |
| --- | --- | --- |
| Enumerate BLAW Canvas pages/modules, extract embedded Echo360 links | `harvestBlaw()` L54, timer L313–316 (300s) | `sync_engine._sync_native_canvas_course` → `canvas.get_course_pages` / `get_course_modules`, `_canvas_echo_urls`, `_persist_canvas_echo_embeds` |
| Paginated Canvas page bodies, module items, assignments | `syncCanvas()` L1334 | `CanvasClient.get_course_pages` (paginated + per-page body hydrate), `get_course_modules`, `get_all_course_assignments` |
| Nested / percent-encoded LTI launch URLs | LTI handshake L463, embed mount watcher L547 | `echo360_ingest.media_uuid_from_url`, `link_id_from_url` (double-unquote), `_persist_canvas_echo_embeds` |
| Per-link identity (`linkId`), not shared tool UUID | ad-hoc `linkId` regex in `get_echo360_sync_targets` | `canonical_embed_key`, `embed_lesson_id`, `link_id_from_url` — single implementation |
| Resolve Echo360 player / media ID via browser | `runAutoResolver()` L145 (60s), `scrapeEchoPage()` L2152 | `Echo360NativeIngestor._ingest_in_context` (navigates launch URL, captures player traffic) + `camoufox_service.resolve_echo360_embed` |
| VTT subtitles | `subtitles/ingest` POSTs L2250, L2366, L2390, L2544 | `echo360_ingest.write_transcript` (atomic, `WEBVTT` + cue validation) |
| Echo360 JSON cue arrays | `ingest_json_transcript` command L2654 | `echo360_ingest.transcript_from_json` / `cues_to_vtt` |
| HLS subtitle renditions (`TYPE=SUBTITLES`, `.vtt`) | `fetch_embed_manifest` L2653, `ingest_vtt_url` L2655 | `parse_hls_subtitle_uris` + `Echo360NativeIngestor` manifest fetch in the same authenticated context |
| Transcript response interception (player XHR hooks) | fetch/XHR monkey-patch L2547–2549 | `page.on("response")` capture of `/transcript`, `/captions`, `.m3u8`, poster URLs |
| Media (video) ingest | `uploadMediaBlob` L2051, `downloadAndIngestStream` L2081 | `Echo360NativeIngestor` records `media_url`/`media_kind` (m3u8) on the canonical record; existing `lectures.ingest_lecture_media` / `Echo360Client.download_media` paths retained |
| Thumbnail / poster capture | L848, L1224, L2433 → `thumbnails/ingest` | `Echo360NativeIngestor` poster response capture → `echo360_ingest.write_thumbnail` (canonical key + media-id fallback) |
| Progress / status heartbeat | `bridge/heartbeat`, `bridge/command` loops L2557–2575 | `sync_status`/`log_sync` + `/native/status`, `/native/coverage`, `/sync/status` |
| Retries / resume | `unisync_attempt_counts`, `unisync_attempt_times`, `reset_resolver` command | `NativeSyncState.pending` + `note_outcome` retry-after, persisted across restarts |
| Periodic scheduling | 15s / 60s / 300s `setInterval` in the tab | `native_sync_scheduler` (startup + 6h default, configurable), `POST /native/schedule` |
| Play / captions nudge (player must start) | `play_player`, `enable_captions` commands L2573–2575 | `Echo360NativeIngestor` clicks `video` / `[class*=play]` / `[id*=play]` after `domcontentloaded` |
| Catalog merge of fetched Echo360 syllabi | (implicit, backend-side) | `persist_syllabus` (merge, never erases cached lessons) + `enrich_embed_from_syllabus` |
| Canvas-material search reindex after sync | bridge-triggered | `sync_engine._index_canvas_materials` after each native course pass |

Not reimplemented natively (retired with evidence):

- Outlook/OWA email scraping (`scrapeOutlookDom` L1728, `syncOutlookEmails` L1957) — unrelated to
  Echo360 ingestion and covered by `api/email_monitor.py`; no native dependency is introduced.
- `reset_resolver` debug command — replaced by queue inspection via `/native/status`.

## 2. Identity rules (enforced by all producers and readers)

- `canonical_embed_key(course, uuid, link_id)` is the only dedupe key for catalog rows.
- With a `link_id`, only `BLAW_EMBED_{uuid}_{link_id[:8]}` may satisfy the embed. The shared tool UUID
  file (`BLAW_EMBED_{uuid}.vtt`) is never consulted — this is the false positive the handoff describes.
- Without a `link_id`, the generic UUID and the resolved `media_id` are both acceptable.
- A transcript is present only when the file parses as WebVTT with at least one timed cue.
- A file that is cue-valid but contains *only* the University copyright disclaimer is **not** a
  transcript. `is_notice_only_vtt(max_seconds=120.0)` (notice markers + total cue span ≤ 120 s)
  rejects it in `read_transcript` (without `allow_notice_only`), `_cue_valid_file`,
  `embed_transcript_path` and `classify_embed` (reason `notice_only`); `write_transcript` refuses to
  downgrade a longer real track.
- Coverage statuses: `transcript`, `missing_transcript` (media resolved, no captions),
  `notice_only` (only a disclaimer track exists — never counted as a transcript),
  `unresolved` (`no_media_url` or `missing_link_identity`).

## 3. Measured coverage (2026-11-01, live cache)

Catalog mtime `2026-10-30T02:56:37Z`; syllabus snapshot mtime `2026-10-30T04:29:16Z`;
216 VTT files in cache, 210 cue-valid, exactly 1 notice-only (see §3.3), 268 thumbnails.

Two independent sources are counted separately: Canvas page/module embeds (`build_coverage`) and
Echo360 syllabus lessons (`build_echo360_coverage`). A course can be fully covered by the direct path
while showing few or no Canvas embeds (e.g. Video Games: Remaking Reality).

The figures below are the last fully recorded native run (`native_sync.json`, finished
2026-10-31T18:08:28Z). That run predates the notice-only guard, so the BLAW cue-valid count may still
include the disclaimer file until the next pass re-validates it.

### 3.1 Canvas embeds

| Course | Links | Resolved media | Cue-valid transcripts | Missing |
| --- | ---: | ---: | ---: | ---: |
| Principles of Business Law (BLAW10001_2026_SM2) | 82 | 61 | 82 | 0 |
| Careers & Employability Community | 1 | 1 | 1 | 0 |
| Disco Community 2026 | 13 | 1 | 12 | 1 |
| Foundations of Computing (COMP10001_2026_SM2) | 2 | 2 | 2 | 0 |
| Fundamentals of Interaction Design (INFO10003_2026_SM2) | 1 | 1 | 1 | 0 |
| **Total** | **99** | **66** | **98** | **1** |

Most of the links → resolved-media gap is metadata-only, not data loss: many BLAW embed rows have an
empty `media_url` yet still carry a cue-valid transcript (the legacy bridge ingested the VTT without
persisting `media_url`).

### 3.2 Echo360 direct syllabus lessons

| Course | Lessons | Cue-valid transcripts | Missing |
| --- | ---: | ---: | ---: |
| Foundations of Computing (COMP10001_2026_SM2) | 58 | 45 | 13 |
| Fundamentals of Interaction Design (INFO10003_2026_SM2) | 13 | 10 | 3 |
| Video Games: Remaking Reality (CCDP10003_2026_SM2) | 13 | 13 | 0 |
| **Total** | **84** | **68** | **16** |

`lesson_stem()` preserves the ISO timestamp dots in lesson ids; the legacy `safe_name()` stripped
them, which previously produced 0/84 matches against the cached VTTs.

Remaining gaps (no transcript exists on disk and none is claimed):

1. `256292` / (no link id) — "3.1 Strategy 1: Mindfulness" (`d5917005-e4da-4959-8790-c23fdc4914e2`),
   `unresolved` / `no_media_url`.
2. `240718` / `de5554c9-cdf5-4ff4-98b2-f9b156ebe79f` — "Seminar 4 Recording and Slides". Media is
   resolved (`1140f7a7-f437-4022-9163-4248fbb9742e`) and the real lecture is 1984.3 s, but only the
   680 B disclaimer track is on disk, so it now reports `notice_only` rather than covered.

The former SWOT VAC gap (`eeabc8c0-c73d-472e-96a1-e7a208ee0c9c`) is closed: a genuine 371-cue,
52,858 B transcript is cached.

A further 16 direct syllabus lessons have no cached cue-valid VTT yet (13 in Foundations of Computing,
3 in Fundamentals of Interaction Design). All gaps are queued by `NativeSyncState.enqueue` and retried
on the next run; they are never counted as covered until a cue-valid VTT is fetched and validated for
the canonical key.

### 3.3 Disclaimer ("notice") tracks are not transcripts

Most Echo360 recordings open with a ~15 s University copyright disclaimer published as its own caption
track. The first native run picked that track and stored it as the lecture transcript.

Evidence, measured 2026-11-01 by summing HLS `#EXTINF` durations:

| Lesson | Real duration | Captured VTT |
| --- | ---: | --- |
| Seminar 4 `de5554c9-cdf5-4ff4-98b2-f9b156ebe79f` (media `1140f7a7-f437-4022-9163-4248fbb9742e`) | 1984.3 s / 199 segments | 680 B, 5 cues, last cue 14.13 s — disclaimer only |
| SWOT VAC `eeabc8c0-c73d-472e-96a1-e7a208ee0c9c` | 980.4 s / 99 segments | genuine track: 371 cues, 52,858 B, last cue 16:18.830 |

The 680 B file is therefore the disclaimer, not a truncated lecture.
`BLAW_EMBED_9381284d-5253-4e5b-b173-fb47a63576cb_de5554c9.vtt` is the only notice-only file on disk;
readers return no transcript for it, so Seminar 4 is honestly reported as uncovered until a real track
is fetched.

The native ingestor now collects every candidate track in the authenticated context and prefers the
longest non-notice one (`_direct_transcript_urls` across the `secure-link-access-datas`,
`public-links`, `lessons` and `media/download` families, plus player-context and in-page URL discovery).

## 4. Phased deprecation

| Phase | State | Gate |
| --- | --- | --- |
| 1. Native path live alongside legacy | **Done** — native scheduler discovers, persists syllabi, ingests transcripts, records coverage | — |
| 2. Readers migrated to canonical identity | **Done** — catalog + sync-target readers use `echo360_ingest` | tests green |
| 3. Legacy routes/runtime available as rollback | **Active** — `/bridge/*`, `/lectures/subtitles|media|thumbnails/ingest`, userscript asset untouched | keep until parity reviewed |
| 4. Retire bridge discovery/resolver | **Not started** — requires every remaining gap to be classified as *no source* or *notice only*, and a manual native run to match 99/99 embed classification plus 84/84 direct lessons | evidence + parity review |
| 5. Delete legacy runtime | **Not started** | phase 4 |

A `notice_only` classification is a legitimate terminal state for phase 4: it means no real caption
track exists, which is not a parity failure.

Rollback: the legacy runtime and endpoints are unmodified and the on-disk caches
(`cache/echo360_subtitles`, `cache/echo360_thumbnails`, `libraries/echo360_embeds.json`,
`cache/echo360_syllabus.json`) remain in their original formats, so switching back needs no migration.

## 5. Operating the native path

- `GET /api/v1/sync/native/status` — scheduler run state, pending queue, last error, login flag.
- `GET /api/v1/sync/native/coverage` — per-course links / resolved media / transcripts / missing reasons.
- `POST /api/v1/sync/native/refresh` — full sync; `{"course_ids":["240718"]}` for a per-course refresh.
- `POST /api/v1/sync/native/schedule` — `{"enabled":true,"interval_seconds":21600}`.
- Session expiry: the pass returns `login_required`, opens the interactive Camoufox login, and leaves the
  queue persisted; after authentication `maintain_canvas_session` resets the next run so work resumes.
- Local run: `cd UniSync && PYTHONPATH=backend "C:/Users/Admin/miniconda3/python.exe" -m pytest backend/tests/test_echo360_native_ingestion.py -q`
  (16 tests). The resident profile lives at `data/browser_profile/camoufox`.
