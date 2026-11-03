# v3 Native Sync Pipeline — Pre-P0 Architecture Snapshot

**Date:** 2026-08-26  
**Git HEAD:** `c17328a6`  
**Branch:** `main`  
**Backup branch:** `backup/pre-p0-sync-2026-08-26`  
**Context:** Snapshot before P0 bug fixes. The v3 Hybrid Sync replaced the v2
dual-mode lease/owner architecture with a simpler model: the Tampermonkey
userscript scrapes directly in the browser and POSTs JSON payloads to the
backend, bypassing HttpOnly cookie restrictions without requiring GM_cookie
access.

---

## 1. Architecture Diagram

```mermaid
flowchart TB
    subgraph Browser["Browser (Tampermonkey v3.0.19)"]
        US["echo360-connector.user.js"]
        US -->|every 15s + 1s initial| CanvasScrape["Strategy: Canvas scrape<br/>fetch /api/v1/courses<br/>fetch /modules?include[]=items<br/>fetch Page body JSON"]
        US -->|on echo360.net.au| EchoScrape["Strategy: Echo360 scrape<br/>GM_xmlhttpRequest → targets<br/>Promise.all(itemTasks)"]

        EchoScrape --> SubA["Subtitle Strategy A<br/>VTT direct download"]
        EchoScrape --> SubB["Subtitle Strategy B<br/>JSON cues endpoint"]
        EchoScrape --> SubC["Subtitle Strategy C<br/>Fallback JSON cues"]
        EchoScrape --> MediaA["Media Strategy A<br/>echoplayer JSON API<br/>(player-properties, media, lesson)"]
        EchoScrape --> MediaB["Media Strategy B<br/>richMedia public page regex"]
        EchoScrape --> MediaC["Media Strategy C<br/>GM_xmlhttpRequest HEAD<br/>redirect to S3/CloudFront"]
    end

    US -->|GM_xmlhttpRequest POST| POST["POST /api/v1/sync/payload"]
    US -->|GM_xmlhttpRequest POST| Cookies["POST /api/v1/sync/credentials<br/>(cookie seeding)"]

    subgraph Backend["Backend (FastAPI sync_engine.py)"]
        POST -->|"canvas_courses"| CanvasHandler["Canvas payload handler<br/>(L84-97)"]
        POST -->|"echo360_sections"| EchoHandler["Echo360 payload handler<br/>(L99-266)"]
        Cookies --> CookieJar["global_cookie_jar<br/>(in-memory dict)"]
        CookieJar -->|"auto-start if both ready"| DeadPath["_run_sync_pipeline<br/>DEAD PATH (L286-335)<br/>native cookie-based sync"]
    end

    subgraph SubtitleCache["Subtitle Cache"]
        EchoHandler -->|"write .vtt / .json"| SubCache["data/cache/echo360_subtitles/<br/>{safe_id}.vtt | .json"]
    end

    subgraph BackgroundProcessing["Background Tasks (FastAPI)"]
        EchoHandler -->|"media_url exists"| DlProc["_download_and_process_echo360<br/>(L534-556)"]
        EchoHandler -->|"no media_url"| DirectProc["_process(item_id)<br/>(L296-407)"]
    end

    subgraph DownloadStage["Download Stage"]
        DlProc --> ValidateURL["_validated_echo360_media_url<br/>(L416-428)"]
        ValidateURL -->|"MP4/MP3 direct"| DirectDL["_download_echo360_media<br/>requests.get stream<br/>(L497-529)"]
        ValidateURL -->|".m3u8 HLS"| HLS["_download_hls_stream<br/>8-thread parallel segments<br/>(L431-494)"]
    end

    subgraph ProcessingPipeline["Processing Pipeline (_process)"]
        DirectDL --> OfficialSubs{"Official subtitles<br/>available?"}
        HLS --> OfficialSubs
        SubCache -.->|"read cache"| OfficialSubs
        DirectProc --> OfficialSubs
        OfficialSubs -->|"yes (cache hit)"| UseCached["Use cached transcript + VTT"]
        OfficialSubs -->|"no (cache miss)"| FFmpeg["ffmpeg -vn -ar 16000<br/>-ac 1 -ab 48k<br/>extract mono MP3"]
        FFmpeg --> Whisper["OpenAI Whisper<br/>transcribe_audio()<br/>(chunked ≤24MB)"]
        Whisper --> Transcript["transcript.txt + subtitles.vtt"]
        UseCached --> Transcript
        Transcript --> Summarize["NotebookSummarizeAgent<br/>→ notes.md"]
        Summarize --> Index["_index_notes()<br/>→ run_upload_processing_task<br/>→ RAG (llamaindex)"]
        Index --> Ready["status: ready"]
    end

    subgraph ProgressPolling["Frontend Progress Polling"]
        ProgressPolling -->|GET 1.5s interval| ProgressAPI["GET /api/v1/sync/progress"]
        ProgressPolling -->|GET 10s interval| CredStatus["GET /api/v1/sync/credentials/status"]
    end
```

### Historical Context: v2 → v3 Migration

**v2 (HANDOFF_CANVAS_AUTOSYNC.md):** Complex lease/owner protocol via
`sync_owner.py`. Tampermonkey userscript acted as an "authenticated owner" that
claimed a lease, downloaded files in-browser, and uploaded them back to the
backend. Required `GM_cookie` for cross-origin cookie access, a WebSocket
takeover protocol, a command queue, and a watchdog timer. Process-local state
meant multi-worker deployments were unsafe.

**v3 (current):** Eliminated the lease/owner/WebSocket infrastructure entirely.
The userscript scrapes in the browser (where cookies are naturally available),
constructs a self-contained JSON payload, and POSTs it to the backend. The
backend never needs the user's raw cookies for the primary path — only the
subtitle cache and media download paths use them. This trades robustness (no
retry/lease/recovery protocol) for simplicity (fewer moving parts, no
GM_cookie dependency).

**Design trade-off rationale:** The HttpOnly cookie bypass was the core
motivation. Echo360 and Canvas both set HttpOnly cookies that JavaScript
cannot read. The v2 approach used `GM_cookie` (Tampermonkey privileged API)
to extract them, but this was fragile across browser updates and required
special permissions. v3 sidesteps the problem entirely: the browser *is* the
authenticated client, so it does the scraping.

---

## 2. Data Flow Table

| Stage | Trigger | Handler | File:Line | Cache/Artifact |
|-------|---------|---------|-----------|----------------|
| **Cookie seeding** | Userscript POST | `sync_credentials()` | `sync_engine.py:49-69` | `global_cookie_jar` (in-memory) |
| **Canvas scrape** | Userscript `fetch()` on canvas domain | `hybridSync()` canvas branch | `echo360-connector.user.js:31-61` | — (POSTed to backend) |
| **Echo360 scrape** | Userscript `fetch()` on echo360 domain | `hybridSync()` echo360 branch | `echo360-connector.user.js:63-260` | — (POSTed to backend) |
| **Subtitle fetch** | Per-item inside `Promise.all(itemTasks)` | `fetch(/media/download/…/transcript.vtt)` + JSON fallback | `echo360-connector.user.js:96-128` | — (attached to syllabus item) |
| **Media URL resolution** | Per-item (3 strategies A/B/C) | JSON API → richMedia page → HEAD redirect | `echo360-connector.user.js:142-250` | — (attached to syllabus item) |
| **Payload receive (Canvas)** | `POST /sync/payload` | `sync_payload()` canvas branch | `sync_engine.py:84-97` | — (logs only, no persistence) |
| **Payload receive (Echo360)** | `POST /sync/payload` | `sync_payload()` echo360 branch | `sync_engine.py:99-266` | `data/cache/echo360_subtitles/{id}.vtt` |
| **Subtitle caching** | Inside echo360 payload handler | `Path.write_text()` | `sync_engine.py:157-167` | `.vtt` or `.json` in cache dir |
| **Metadata creation** | Inside echo360 payload handler | `_write(item_dir, meta)` | `sync_engine.py:195-213` | `media_library/{uuid}/metadata.json` |
| **Re-queue existing** | If `lesson_id` in `existing_ids` but no media file | `_download_and_process_echo360()` | `sync_engine.py:224-257` | Updates `metadata.json` status |
| **Download** | `background_tasks.add_task(_download_and_process_echo360)` | `_download_echo360_media()` | `lectures.py:497-529` | `media_library/{uuid}/media.mp4` |
| **HLS download** | If URL ends `.m3u8` | `_download_hls_stream()` | `lectures.py:431-494` | 8-thread parallel, concat `.ts` |
| **Subtitle check** | `_process()` entry | `_fetch_echo360_official_transcript()` | `lectures.py:171-242` | Reads cache, falls back to HTTP |
| **FFmpeg extraction** | No official subtitles | `_extract_and_chunk_audio()` | `lectures.py:247-293` | `media_library/{uuid}/_tmp_audio/extracted.mp3` |
| **Whisper transcription** | No official subtitles | `transcribe_audio()` | `lectures.py:319-350` | `media_library/{uuid}/transcript.txt` |
| **Summarization** | After transcript ready | `NotebookSummarizeAgent().summarize()` | `lectures.py:366-374` | `media_library/{uuid}/notes.md` |
| **Subtitle write** | After transcript ready | `vtt_content.write_text()` | `lectures.py:378-381` | `media_library/{uuid}/subtitles.vtt` |
| **KB indexing** | After notes ready | `_index_notes()` → `run_upload_processing_task()` | `lectures.py:391` | RAG knowledge base |
| **Progress polling** | Frontend 1.5s interval | `GET /sync/progress` | `sync_engine.py:27-30` | `sync_status` dict |
| **Credential status** | Frontend 10s interval | `GET /sync/credentials/status` | `sync_engine.py:33-47` | bool flags |

---

## 3. Concurrency Map

### Sequential (currently)

| Operation | Why Sequential | Bottleneck? |
|-----------|---------------|-------------|
| Canvas course crawl | `for course in courses: await canvas.get_course_modules(course_id)` | **YES** — one HTTP roundtrip per course |
| Canvas module item fetch | Nested `for mod in modules` loop (userscript) | **YES** — sequential per module per course |
| Echo360 syllabus per target | `for target of targets: fetch(/section/…/syllabus)` (userscript) | Moderate — targets are few |
| `_download_and_process_echo360` | Each lecture runs as independent `background_tasks.add_task()` | **NO semaphore** — unbounded |
| `_process()` stages | `transcribe → summarize → index` is sequential within one item | By design — depends on prior output |
| Whisper chunked transcription | `for chunk_path, time_offset in chunk_tuples: transcribe_audio()` | Sequential within a single lecture |

### Parallel

| Operation | Mechanism | Concurrency Level |
|-----------|-----------|-------------------|
| Echo360 item processing (subtitles + media resolve) | `Promise.all(itemTasks)` in userscript | **Unbounded** — all syllabus items at once |
| HLS segment download | `ThreadPoolExecutor(max_workers=8)` | 8 threads |
| Frontend progress polling | `setInterval` 1.5s | Single browser tab |
| Background task dispatch | FastAPI `BackgroundTasks` | **No limit** — one task per lecture queued |
| Echo360 payload POST → background tasks | All lectures queued in single POST handler | All at once on server |

### Current Bottlenecks Summary

1. **No semaphore on background tasks:** Every lecture in an Echo360 section
   gets a `background_tasks.add_task()` with no rate limit. A 30-lecture course
   spawns 30 concurrent downloads + transcriptions. This can exhaust disk I/O,
   memory (ffmpeg processes), and Whisper API rate limits.

2. **Serial course/module fetch (Canvas):** `canvas.get_courses()` is called
   once, but `get_course_modules()` is called per-course sequentially. With
   10+ courses, this adds 10+ serial HTTP roundtrips before any processing
   starts.

3. **15-second full re-scrape:** The userscript runs `setInterval(hybridSync,
   15000)` — a full Canvas + Echo360 scrape every 15 seconds. This means
   duplicate POST payloads hit the backend every 15s. The dedup check
   (`_existing_echo360_ids`) prevents re-import but not re-processing of the
   POST handler overhead.

4. **No debounce on payload receipt:** `sync_payload()` clears `sync_status`
   and re-processes every time. If two rapid POSTs arrive (Canvas + Echo360),
   the second one overwrites `sync_status["log"]` from the first.

---

## 4. Known Bugs & Limitations (P0)

### P0: `urljoin` Missing Import

**File:** `deeptutor/api/routers/lectures.py:447,456`  
**Issue:** `_download_hls_stream()` calls `urljoin()` (stdlib `urllib.parse.urljoin`)
but the module only imports `from urllib.parse import urlparse`. The `urljoin`
name is never imported.  
**Impact:** Any HLS (`.m3u8`) download will crash with `NameError: name 'urljoin' is not defined`.
Only MP4/MP3 direct downloads succeed.  
**Fix:** Add `urljoin` to the existing `from urllib.parse import urlparse` on line 15.

### P0: Debug JSON Dump Left in Production Path

**File:** `deeptutor/api/routers/sync_engine.py:119-121`  
**Issue:** Every Echo360 payload write unconditionally dumps the entire
`sections` array to `scratch_echo360_payload.json` on disk.  
**Impact:** (a) Writes potentially large JSON files to the project root on
every sync cycle. (b) File is never cleaned up. (c) Appears in `git status`
as untracked. (d) Could contain sensitive lecture metadata.  
**Fix:** Remove the `json.dump` block or gate it behind a `DEEPTURTOL_DEBUG`
environment variable.

### P0: Race Conditions on Global Mutable State

**File:** `deeptutor/api/routers/sync_engine.py:12-17`  
**Issue:** `global_cookie_jar` and `sync_status` are plain dicts mutated by
concurrent request handlers and background tasks without any locking.  
**Specific race:** `sync_payload()` sets `sync_status["is_syncing"] = True`
(L81) and `sync_status["log"].clear()` (L82), while `_run_sync_pipeline()`
does the same (L294-295). If both paths execute concurrently (e.g., cookie
auto-start triggers `_run_sync_pipeline` while a `sync_payload` POST arrives),
the progress/log state is corrupted.  
**Impact:** Incorrect progress display, potential KeyError on concurrent dict
mutation.  
**Fix:** Use `asyncio.Lock` or thread the state through a proper state object
with atomic transitions.

### P0: Unbounded Concurrency

**Issue:** `sync_payload()` (L216-218) calls `background_tasks.add_task()`
once per lecture with no throttling. A single Echo360 section with 40 lectures
spawns 40 concurrent download+process pipelines. Each pipeline runs ffmpeg
(subprocess) and Whisper (HTTP API call).  
**Impact:** Memory exhaustion (multiple ffmpeg processes), Whisper API 429s,
disk thrashing from parallel HLS segment downloads.  
**Fix:** Add an `asyncio.Semaphore(N)` (e.g., N=3) to gate background task
execution, or use a proper task queue (e.g., `asyncio.Queue` with worker
coroutines).

### P0: `_run_sync_pipeline` is a Dead Path

**File:** `deeptutor/api/routers/sync_engine.py:286-335`  
**Issue:** The native Python sync pipeline (`_run_sync_pipeline`) creates
`CanvasClient`/`Echo360Client` with cookies from the jar, but:
- Canvas courses are fetched but items are never downloaded or indexed (L315:
  `pass`)
- Echo360 syllabus is fetched but not processed (no download/summarize/index)
- The pipeline only reads and logs; it never persists anything to the media
  library  
**Impact:** `POST /sync/start` (L269-284) triggers this pipeline, which does
nothing useful. The UI "Sync now" button calls this endpoint, but real work
happens via the `POST /sync/payload` path from the userscript. The dead path
also silently consumes cookies from the jar when they might be needed elsewhere.

### Limitation: 15s Full Re-scrape

**File:** `echo360-connector.user.js:278`  
**Issue:** `setInterval(hybridSync, 15000)` triggers a complete Canvas +
Echo360 scrape every 15 seconds, regardless of whether a sync is already in
progress. The backend deduplicates imports via `_existing_echo360_ids()`, but
the scraping overhead (browser network, CPU, backend POST processing) is
unnecessary.  
**Fix:** Check `sync_status.is_syncing` before scraping, or increase interval
to 60s+, or only re-scrape on explicit user action.

### Limitation: No urljoin in userscript media URL construction

**File:** `echo360-connector.user.js:228`  
**Issue:** The `GM_xmlhttpRequest` HEAD strategy (Strategy C) constructs URLs
using template literals without resolving relative paths. This works because
the URLs are always absolute, but the pattern is fragile if Echo360 changes
their URL structure.

### Limitation: Cookie Jar is Process-Local

**File:** `sync_engine.py:12`  
**Issue:** `global_cookie_jar` is an in-memory dict. Backend restart clears
it. Users must re-seed cookies via the userscript after every restart.
The `_run_sync_pipeline` auto-start on cookie receipt partially mitigates this
but triggers the dead path.

### Limitation: Canvas Payloads Not Persisted

**File:** `sync_engine.py:84-97`  
**Issue:** The Canvas `canvas_courses` payload handler only logs statistics
and updates `sync_status`. It does not write course/module/page data to the
media library or any knowledge base. Canvas content ingestion via the hybrid
path is effectively a no-op.

---

## 5. Pre-P0 Metrics Snapshot

### Data as of verification run

| Metric | Value | Source |
|--------|-------|--------|
| Subtitles cached (.vtt + .json) | 29 | `data/cache/echo360_subtitles/` |
| Media library items (metadata.json) | 31 | `media_library/*/metadata.json` |
| Downloaded media files (media.mp4) | 11 | Items with `media.mp4 > 0 bytes` |
| Media.mp4 sizes | Various (see below) | Filesystem |

### Progress log sample (typical sync cycle)

```
Discovered N Canvas courses via Hybrid browser scrape!
[COURSE_NAME] Found M modules.
[COURSE_NAME] Indexed P rich text pages!
Hybrid Canvas Sync Complete!
Discovered X Echo360 sections via Hybrid browser scrape!
  - Echo360 Section SEC_ID has Y lectures.
  - Downloaded Z subtitle tracks (.vtt) securely!
  - Queued W lecture(s) for async download & RAG indexing!
Hybrid Echo360 Sync Complete! (Z subtitles cached, W lectures queued for download)
```

### Sync status endpoint sample

```json
{
  "is_syncing": false,
  "progress": {
    "message": "Hybrid Echo360 Sync Complete! (29 subtitles cached, 11 lectures queued)",
    "done": 100,
    "total": 100
  },
  "log": ["Discovered 10 Canvas courses...", "..."]
}
```

### Credential status endpoint sample

```json
{
  "has_canvas_cookies": true,
  "has_echo360_cookies": true,
  "total_cookies": 14
}
```

---

## 6. Rollback Instructions

### Option A: Restore specific files from backup branch

```bash
# Restore sync engine to pre-P0 state
git checkout backup/pre-p0-sync-2026-08-26 -- deeptutor/api/routers/sync_engine.py

# Restore lectures.py (HLS pipeline + processing)
git checkout backup/pre-p0-sync-2026-08-26 -- deeptutor/api/routers/lectures.py

# Restore the userscript
git checkout backup/pre-p0-sync-2026-08-26 -- deeptutor/assets/echo360_userscript/echo360-connector.user.js
git checkout backup/pre-p0-sync-2026-08-26 -- web/public/echo360-connector.user.js

# Restore canvas/echo360 clients
git checkout backup/pre-p0-sync-2026-08-26 -- deeptutor/services/sync/canvas_client.py
git checkout backup/pre-p0-sync-2026-08-26 -- deeptutor/services/sync/echo360_client.py

# Restore library UI
git checkout backup/pre-p0-sync-2026-08-26 -- "web/app/(workspace)/library/page.tsx"
```

### Option B: Restore from checkpoints directory

```bash
# Checkpoints exist at checkpoints/pre-p0-2026-08-26/
# Runtime state snapshot (for reference, not code restore)
cat checkpoints/pre-p0-2026-08-26/runtime-state/git-status.txt
cat checkpoints/pre-p0-2026-08-26/runtime-state/git-log.txt
```

### Option C: Full branch rollback (nuclear)

```bash
# Create a revert branch
git checkout -b revert-p0-fixes backup/pre-p0-sync-2026-08-26
# Cherry-pick only non-sync changes if needed
```

---

## 7. Key File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `deeptutor/api/routers/sync_engine.py` | 1-343 | Sync API endpoints, cookie jar, payload handler, dead native pipeline |
| `deeptutor/api/routers/lectures.py` | 1-854 | Media library CRUD, Echo360 import, download, HLS, transcription, summarization, KB indexing |
| `deeptutor/services/sync/canvas_client.py` | 1-58 | httpx-based Canvas LMS API client (used only by dead `_run_sync_pipeline`) |
| `deeptutor/services/sync/echo360_client.py` | 1-58 | httpx-based Echo360 API client (used only by dead `_run_sync_pipeline`) |
| `deeptutor/assets/echo360_userscript/echo360-connector.user.js` | 1-280 | Tampermonkey v3.0.19 userscript — browser-side scraping + payload POST |
| `web/app/(workspace)/library/page.tsx` | 780-900 | Frontend progress polling, credential status check, sync trigger |
| `DOCS/HANDOFF_CANVAS_AUTOSYNC.md` | 1-268 | v2 lease/owner architecture handoff (historical reference) |

---

*Document generated as architecture snapshot before P0 bug fixes. See
AGENTS.md for agent code commenting guidelines.*
