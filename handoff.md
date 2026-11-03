# UniSync Session Handoff — 2026-09-12

## Current State

- **Branch**: `master`
- **Latest Commit**: `fix(thumbnails,xml)` — escape XML special characters in SVG posters, enhance module sibling matching, and fix client fallback data-URI
- **Key Milestones**:
  - `fix(thumbnails,xml)` — escape XML special characters in SVG posters, enhance module sibling matching, and fix client fallback data-URI
  - `feat(blaw,thumbnails)` — link 22 real cached Echo360 JPEGs to BLAW embeds, fix INFO10003 fallback corruption, and add userscript poster extraction
  - `feat(player,thumbnails)` — remove in-app player for direct site launch, overhaul lecture thumbnails with course-branded posters, and silence auto-resolver tabs
  - `fix(catalog)` — resolve Echo360 Lecture placeholder to 8.1 Vitiating Factors and add dynamic player title extraction
  - `feat(edstem)` — automatic full ingestion of lessons, quiz questions/solutions, discussions, file downloads, deadlines, and dynamic course library auto-provisioning
  - `60fda58` — docs: establish repository documentation standard and add comprehensive walkthrough
  - `2eea4c2` — fix(catalog): prioritize module numbering in deduplication and make harvested embeds visible
  - `6d430da` — fix(catalog): resolve single-lecture truncation for CCDP and INFO by preserving weekly dates and cleaning timetable titles
  - `3d4d132` — fix(player,catalog): resolve in-app player failure by requiring real media files on disk and enabling direct authenticated stream launch
  - `8017722` — fix(gallery,player): canonical semester sorting, deduplication, and multi-mode in-app player
  - `13647f8` — fix(catalog,resolver): clean titles, real course dates, working launch links, and kill auto-resolver tab loop
- **Tests**: **216 passed, 0 failed** (`pytest backend/tests/ -q` in 29.96s, 100% green)
- **Server**: Active on `http://127.0.0.1:8000` (`python -m uvicorn unisync.api.main:app --app-dir backend --port 8000`)
- **Detailed Architecture & Troubleshooting Guide**: See [docs/WALKTHROUGH.md](file:///C:/Users/Admin/Desktop/UniSync/docs/WALKTHROUGH.md)

---

## What Was Done

### 1. SVG XML Well-Formedness Fix & Enhanced Sibling Thumbnail Resolution
- **Root Causes**:
  1. Bare ampersands `&` in departmental subheadings (`Melbourne Law School & Business`, `School of Computing & Information Systems`, etc.) caused browser XML parsers to reject generated SVG posters with fatal parse errors (`not well-formed (invalid token)`), displaying broken image boxes.
  2. Client-side `onerror` fallback SVG in `dashboard.html` lacked URL-percent-encoding (`encodeURICompenent`), causing the fallback itself to fail.
  3. Sibling title matching in `get_library_thumbnail` was case-sensitive and failed on `.mp4` extensions and case variations (e.g. `8.1 Vitiating factors` vs `8.1 Vitiating Factors`).
- **Resolution**:
  - Escaped XML entities in `_generate_thumbnail_svg()` (`&` -> `&amp;`, `<` -> `&lt;`, `>` -> `&gt;`).
  - Added module regex matching (`\b[1-9]\.[0-9]+\b`) and normalized title comparison, unlocking 23 additional real Echo360 JPEGs from disk (including 8.1, 2.2, 3.3, 1.3, 4.2, 4.7, 6.1, 6.2, 6.3). Total BLAW real JPEGs increased from 22 to 45.
  - Properly URI-encoded `fallbackSvg` in `dashboard.html` and bumped cache-buster to `?v=5`.
- **Live Verification**:
  - `8.1`: HTTP 200 `image/jpeg` (106,364 bytes, real screenshot JPEG)
  - `8.2`, `8.3`, `8.4`: HTTP 200 `image/svg+xml` (100% valid XML, zero parse errors)
  - Full test suite: 216 / 216 passing.

### 2. Auto-Resolver Tab Silencing & Target Exclusion
- **Root Cause**: `get_echo360_sync_targets()` required raw `.m3u8` URLs even if `.vtt` subtitles/transcripts were already downloaded to disk. The 5-minute attempt wipe in `bridge-runtime.js` repeatedly spawned tabs with `{ active: true }`.
- **Backend Fix**: Updated exclusion condition in `backend/unisync/api/lectures.py` to `if is_downloaded or has_sub or item.get("media_url"): continue`, and guarded default practice test bootstrapping.
- **Userscript Fix**: Updated filter in `bridge-runtime.js` to `!t.downloaded && !t.subtitle_available && !t.media_url`, enforced strict 1-attempt ceiling with 24-hour backoff (`86400000ms`), and set `{ active: false }`.
- **Live Verification**: `GET /api/v1/lectures/libraries/echo360-sync-targets` returns `sync_targets: 0, embed_targets: 0`.

### 2. Complete Removal of In-App Streaming Player
- **Excised Player Modal**: Completely removed `#player-modal`, `<video id="player-video">`, and canvas notices from `backend/unisync/assets/dashboard.html`.
- **Direct Website Launch**: Replaced modal popup triggers with direct calls to `window.open(watchUrl, '_blank')` on hover play button, lecture title, and action link.
- **Removed CDN Dependency**: Removed `hls.js` CDN script and cleaned modal Escape key listeners.

### 3. Missing & Broken Thumbnail Overhaul
- **Syllabus Media ID Matching**: `get_library_thumbnail` in `backend/unisync/api/lectures.py` cross-references `echo360_syllabus.json` lesson `media_id`s, allowing 42 syllabus lessons to match cached `mid.jpg` in `echo360_thumbnails/`.
- **Course-Branded Vector Posters**: Upgraded `_generate_thumbnail_svg()` to generate course-branded posters with subject color palettes (Sky Blue for COMP, Amber for BLAW, Violet for INFO, Emerald for CCDP), two-line wrapped typography, glassmorphism play discs, and department subheadings.
- **Error Fallbacks & Safe Encoding**: Encoded lecture IDs in thumbnail URLs (`encodeURIComponent(lec.id)`) and replaced recursive broken-image retry loops with inline SVG data-URIs on both lecture cards and enrolled course banners.

### 4. Automatic Full EdStem Course Ingestion
- **Interactive Quiz Questions & Solutions**: Automatically extracts questions, options, solution indices, and explanations from `lessons/slides/{id}/questions` and formats with `[x]` / `[ ]` checked state.
- **Rich XML Document Linearizer**: Supports EdStem canonical tags (`<paragraph>`, `<heading level="N">`, `<bold>`, `<italic>`, `<callout>`, `<break>`, `<list>`, `<list-item>`, `<snippet-file>`, `<link>`, `<image>`, `<math>`).
- **Binary Resource Downloader**: POST download with Bearer authentication and size-based caching.
- **Dynamic Course Library Auto-Provisioning**: Auto-matches course codes or provisions new library entries in `libraries.json`.
- **Server Startup & Sync Engine Integration**: Background sync fires automatically on server startup and whenever Canvas sync runs.

### 5. Elimination of "Echo360 Lecture" Placeholders & Dynamic Player Title Extraction
- **Identified Orphan Video**: Identified embed `79f0af68-f485-4775-b0df-9c551acd52ec` as BLAW10001 Video 8.1 (*8.1 Vitiating Factors Overview 2026*).
- **Dynamic Player `<title>` Fetching**: Implemented `_fetch_title_from_player_url` with in-memory memoization to automatically extract authentic lecture titles from Echo360 player HTML pages on ingestion and catalog resolution.
- **Canonical Deduplication**: Bridged 8.1's Canvas LTI embed and standalone player URL so they merge canonically into a single unified card.

---

## Ground Truth Verification

```powershell
# Run full automated test suite (215 passing)
pytest backend/tests/ -q

# Verify sync targets are silenced (returns 0 targets)
python -c "from unisync.api.lectures import get_echo360_sync_targets; print(get_echo360_sync_targets())"
```


