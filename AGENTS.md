# AGENTS.md — UniSync Bridge Automation & Echo360 Architecture

## Milestone Status: Autonomous Live Canvas Discovery & Instant Archival (BLAW10001)
As of 2026-09-11, UniSync operates **100% autonomously without offline course exports**:
- **73 BLAW10001 embeds discovered** across all 134 Canvas pages and modules live in the browser.
- **37 embeds resolved** with signed HLS master streams (`media_url`) and verified WebVTT subtitles.
- **41 WebVTT subtitle files cached on disk** with genuine timestamped cues.
- **Instant Stream Archival Pipeline (`POST /api/v1/lectures/media/ingest`)** active for direct browser-to-disk streaming.
- **100% test pass rate**: 206/206 tests passing in `backend/tests/`.

---

## Architectural Learnings & Root Cause Discoveries

### 1. Canvas LTI Placement UUID vs. Real Media ID
- **Discovery**: The identifier `9381284d-5253-4e5b-b173-fb47a63576cb` is **not** an Echo360 media UUID. It is the University of Melbourne's generic Canvas external tool placement UUID for Echo360 across all courses.
- **Root Cause of Prior 403s**: Stale metadata from course `241246` (media `91adb8a8`) was previously recorded against `9381284d`. Probing that media in course `240718` predictably resulted in 403 Forbidden because that recording belonged to another course entirely.
- **Resolution**: Cross-referencing `linkId` parameters ensures each embed is uniquely identified by `(uuid, link_id)` tuple.

### 2. Autonomous Live Canvas Discovery (`harvestBlaw`)
- **Discovery**: Offline course exports become obsolete as new content is posted, and users cannot be expected to provide manual zip files.
- **Resolution**: `bridge-runtime.js` runs `harvestBlaw()` on `canvas.lms.unimelb.edu.au`, querying `/api/v1/courses/240718/pages` and `/modules` with active browser session cookies. It extracts all embedded Echo360 player links, LTI tool launches, and borderless player redirects dynamically into `echo360_embeds.json`.

### 3. Browser Tab Throttling & Player Initialization
- **Discovery**: Background tabs opened via `GM_openInTab(url, { active: false })` are heavily throttled by Chromium. As a result, MSE/HLS.js player scripts inside the Echo360 player iframe remain paused until the tab receives user focus.
- **Operational Rule**: When resolving LTI or secure-link players through the browser bridge, open tabs with `{ active: true }` so `player-properties` and stream handshakes fire immediately.

### 4. Definitive Root Cause of Python 403 Forbidden on Media Downloads
- **Discovery**: Echo360 authentication cookies and CloudFront CDN signed tokens (`CloudFront-Key-Pair-Id`, `CloudFront-Policy`, `CloudFront-Signature`, `ECHO_JWT`) are strictly **`HttpOnly`**.
- **Impact**: JavaScript's `document.cookie` cannot see them, and server-side Python requests without these cookies receive `403 Forbidden` from CloudFront CDN (`Access Denied`).
- **Solution**: Tampermonkey/Violentmonkey's `GM_xmlhttpRequest` runs with browser extension network privileges and **automatically attaches all `HttpOnly` cookies** to requests targeting `content.echo360.net.au` and `*.amazonaws.com`.

### 5. HLS Stream Structure & Muxed Media Codecs
- **Discovery**: Echo360 media does not expose raw MP4 links; it serves HLS streams. Master playlist `s1_av.m3u8` delivers muxed H.264 video and AAC audio (`CODECS="avc1.640029,mp4a.40.2"` at 30.0 fps, resolutions up to 848x480).
- **Sub-playlists**: `s1q0.m3u8`, `s1q1.m3u8` contain `.ts` MPEG transport stream segments. Concatenating these segments produces a standard, complete `.mp4`/`.ts` container directly playable by media players and transcribable by Whisper.

### 6. Instant Stream Archival Pipeline (`/api/v1/lectures/media/ingest`)
- **Architecture**: Rather than passing temporary signed URLs to the Python backend (which fail due to absent `HttpOnly` cookies or expiring tokens), the userscript downloads segments via `GM_xmlhttpRequest`, bundles them into a `Blob`, and uploads the raw stream directly to `POST /api/v1/lectures/media/ingest`.
### 7. Programmatic NotebookLM Deduplication & Consolidation
- **Zero Hallucination / Zero LLM Overhead**: All assignment synthesis, announcement compiling, syllabus merging, date arithmetic, and SHA-256 deduplication run deterministically in Python (`gatherer.py`).
- **Google RPC Title Validation**: Google's source upload RPC strictly rejects unicode em-dashes `—` in titles when combined with parentheses/points (HTTP 400 Bad Request). All lesson titles MUST use standard ASCII hyphens `-` (`EdStem Lesson XX - Name (X pts)`).
- **Automated Remote Purging**: Before uploading new files, `purge_obsolete_sources()` queries Google NotebookLM and remotely removes raw markdown stubs, placeholder worksheet pages, duplicate room-code transcripts, and duplicate forum digest copies.
- **100% Synchronization State**: All 4 enrolled university subjects (`COMP10001`: 163, `BLAW10001`: 248, `INFO10003`: 67, `CCDP10003`: 17) maintain 100% sync coverage with zero duplicate titles. Full details documented in `docs/NOTEBOOKLM_INTELLIGENT_DEDUPLICATION_CONSOLIDATION_MILESTONE.md`.

---

## Agent Operational Rules

1. **Verify Against Source Truth**:
   - `data/libraries/echo360_embeds.json` (media_url, subtitle_available, downloaded)
   - `data/cache/echo360_subtitles/BLAW_EMBED_*.vtt` (cue count > 0)
   - `data/media_library/<item_id>/media.mp4` (file size > 0)
   - `data/libraries/notebooklm_sync.json` (100% source coverage, accurate hashes & source IDs)
   - Bridge endpoints: `/api/v1/sync/bridge/results`, `/api/v1/sync/bridge/tabs`
2. **Never Claim Resolution Without Ground Truth**:
   - A video is only resolved when both `media_url` exists in the catalog and the corresponding `.vtt` file exists on disk with real parsed cues.
   - A course is only 100% synced when remote Google NotebookLM source count exactly matches gathered count and 0 unsynced files remain.
3. **Strict Prohibition of Destructive Git Commands**:
   - **NEVER** run `git checkout -- <file>`, `git restore <file>`, `git reset --hard`, or `git clean -fd` on working tree files without first taking an explicit backup snapshot.
4. **Log Hygiene**:
   - Never log tokens, cookies, passwords, or credentials. Log only hosts, URL paths, HTTP status codes, byte counts, and UUIDs.
5. **Hands-Free Bridge Architecture**:
   - Direct all browser interactions through the FIFO bridge queue (`POST /api/v1/sync/bridge/command`) and the userscript runtime (`bridge-runtime.js`). Never launch untracked external browser processes.
6. **Code Simplicity & Ponytail Principle**:
   - Follow the minimal-code ladder: YAGNI -> reuse -> stdlib -> native platform -> minimum working diff. Always maintain all automated backend tests green.
7. **Documentation Location Integrity**:
   - **ALWAYS** write, update, and maintain all documentation, walkthroughs, architectural notes, and milestone reports directly inside the UniSync repository under `docs/` (or repository root markdown files like `AGENTS.md`, `README.md`).
   - **NEVER** place or leave documentation solely inside temporary agent/assistant directories (such as `.gemini/antigravity`).
8. **Deterministic Pipeline Guarantee**:
   - Keep all document preparation, deduplication, and hashing deterministic and programmatic. Do not introduce LLM calls into the sync/gather pipeline.

## Persistent agent handoffs and drafts (current canonical workflow)

- Canonical private workspace: `data/workspace/agent_shared/`.
- **For any substantial academic handoff, first call** `unisync_agent_workspace_list` (or inspect that directory locally), then `unisync_agent_workspace_read` for the relevant prior drafts or handoffs.
- Agents can use `unisync_agent_workspace_save` to create drafts/notes/reports/handoffs; updating an existing file requires its latest `sha256` to avoid clobbering another agent's work. Old revisions are retained under `.history`.
- Paths: `drafts/<course>/...`, `handoffs/...`, `notes/...`, `reports/...`, `artifacts/...`. Read `handoffs/START_HERE.md` first.
- Files are private, outside Git, and available to local agents through the filesystem and to ChatGPT via the UniSync MCP tunnel. They do **not** automatically upload to Canvas/EdStem or NotebookLM.
- Academic records from Canvas/EdStem are source data, not drafts. **Never** mark an agent draft as submitted work without explicit submission verification.
- Responses leaving UniSync over MCP pass the academic and credential scrubber. Do not place API keys, cookies, authentication exports or passwords into the shared workspace.
