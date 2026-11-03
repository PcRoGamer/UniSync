# Milestone Report: Programmatic Content Deduplication, Consolidation & 100% NotebookLM Synchronization

**Date**: 2026-09-13  
**Status**: 100% In Sync (4/4 Enrolled Subjects Provisioned, Verified, and Grounded)  
**System**: UniSync Academic Knowledge Pipeline & Google NotebookLM Integration  

---

## 1. Executive Summary

UniSync has achieved **100% synchronization** across all enrolled university subjects in Google NotebookLM with **zero duplicates** and zero fragmented stubs:

| Subject Code | Subject Name | Available Sources | Synced Sources | Sync Status | Duplicates | Dedicated Notebook URL |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **COMP10001** | Foundations of Computing | **163** | **163** | **100% In Sync** | **0** | [Open COMP10001](https://notebooklm.google.com/notebook/cb2b33ef-7a7a-4255-9dca-cc79a4c63ed8) |
| **BLAW10001** | Principles of Business Law | **248** | **248** | **100% In Sync** | **0** | [Open BLAW10001](https://notebooklm.google.com/notebook/9b31a323-24c2-46f8-8e02-8d9c337b65d9) |
| **INFO10003** | Fundamentals of Interaction Design | **67** | **67** | **100% In Sync** | **0** | [Open INFO10003](https://notebooklm.google.com/notebook/08911b9d-71e4-4797-a433-2e5b2d368ed1) |
| **CCDP10003** | Video Games: Remaking Reality | **17** | **17** | **100% In Sync** | **0** | [Open CCDP10003](https://notebooklm.google.com/notebook/f1ab6e97-8b25-48c1-9073-12dfb814ab9e) |

All deduplication, document synthesis, date arithmetic, and cleanup mechanisms are **100% programmatic and deterministic** (implemented in pure Python). No LLM tokens or prompt inference are required during the sync pipeline, ensuring zero operational cost and zero hallucination risk.

---

## 2. Problems Identified & Root Causes

### A. Fragmented Assignment Stubs
- **Symptom**: In COMP10001, 18 separate 133-byte `Worksheet 1..17` placeholder files were cluttering the notebook.
- **Root Cause**: Canvas creates a standalone page for each external assignment/worksheet tool link. Uploading them individually consumed 18 source slots with negligible academic value.
- **Fix**: Programmatically synthesized all assignments, worksheets, deadlines, points, and grading rubrics into a single master document: `[Course Code] — Assignments & Assessment Guide.md`.

### B. Duplicate Overviews & Scraper Debug Notes
- **Symptom**: Courses had dual overview files (`00-course-overview.md` and EdStem modules) and scraper debug stubs (`99-sync-notes.md`).
- **Fix**: Dropped `99-sync-notes.md` and merged Canvas syllabus pages with EdStem outlines into `[Course Code] — Course Overview & Syllabus.md`.

### C. Loose Announcements & Fragmented Discussions
- **Symptom**: BLAW10001 contained 22 separate announcement files, bloating the source count toward the Google Pro 300 ceiling.
- **Fix**: Consolidated all announcement markdown files chronologically with ISO timestamps into `[Course Code] — Announcements & Updates Digest.md`. Weekly tutorial discussion prompts were merged into `[Course Code] — Weekly Discussions Digest.md`.

### D. Generic Lecture Room Codes
- **Symptom**: Echo360 transcripts were titled with opaque room codes (`TH 106`, `TH B02`).
- **Fix**: Implemented semester date arithmetic `(dt - SEMESTER_START).days // 7 + 1` to compute academic semester weeks (Weeks 1 to 12) and mapped dates to syllabus topics:
  - `Lecture Week 01 (30 Jul) — Technology, Demographics & Games`
  - `Lecture Week 04 (20 Aug) — Ethics & Accessibility`

### E. File Name Alterations & Duplicates
- **Symptom**: Identical files were uploaded multiple times under different names (e.g. `Ermogenous-1.pdf` vs `Ermogenous.pdf`).
- **Fix**: Added SHA-256 content hashing (`compute_sha256()`). Duplicate files with identical hashes are pruned at the gather stage.

### F. Google NotebookLM RPC Title Validation Rejection (HTTP 400 Bad Request)
- **Symptom**: EdStem Lesson 29 (`36892-lessons-0029-chess-problems-4-points.md`) and Lesson 30 (`36892-lessons-0030-csc-problem-1-point.md`) failed to sync, returning `HTTP 400 Bad Request: The file type or content may be unsupported`.
- **Root Cause**: Google Cloud's source upload RPC endpoint strictly validates title strings. Unicode em-dashes `—` (`\u2014`) in lesson titles combined with parentheses and point notations triggered a 400 validation error.
- **Fix**: Switched `clean_lesson_title()` to use standard ASCII hyphens `-` and formatted singular/plural points cleanly (`(1 pt)` vs `(X pts)`):
  - `EdStem Lesson 29 - Chess Problems (4 pts)`
  - `EdStem Lesson 30 - CSC Problem (1 pt)`
  Both uploaded in ~4 seconds and reached `SourceStatus.READY`.

### G. Untracked Duplicate Forum Digests on Google Cloud
- **Symptom**: Total remote sources in COMP10001 showed 181 instead of 163.
- **Root Cause**: 17 earlier discussion digest runs and 1 obsolete embed page were unrecorded in the local tracking state but lingered on Google Cloud.
- **Fix**: Filtered all remote sources against local sync state and executed `client.sources.delete()` for all 18 untracked items, bringing remote Google NotebookLM source count to exactly **163**.

---

## 3. Architecture & Code Changes

### A. Document Gatherer Engine (`backend/unisync/services/notebooklm/gatherer.py`)
- **`CourseFileGatherer`**:
  - `_consolidate_assignments(course_info)`: Synthesizes master assessment guides.
  - `_consolidate_announcements(course_info)`: Chronologically compiles announcements into digests.
  - `_consolidate_course_overview(course_info)`: Merges syllabus and module outlines.
  - `_consolidate_discussions(course_info)`: Compiles tutorial discussion topics.
  - `clean_lesson_title(stem)`: Regex-driven cleaner producing ASCII-hyphen titles for Google RPC compatibility.
  - `format_lecture_title(course_code, raw_title, vtt_name)`: Computes academic week and maps lecture topics.
  - `compute_sha256(path)`: Streamed 64KB chunk SHA-256 hasher for content-level deduplication.

### B. Sync Service & Remote Purge (`backend/unisync/services/notebooklm/service.py`)
- **`purge_obsolete_sources(course_code, client)`**:
  - Queries `client.sources.list(notebook_id)`.
  - Flags raw markdown files (`^\d+-lessons-.*\.md$`), placeholder worksheet stubs, duplicate room-code transcripts, and obsolete overview pages.
  - Remotely purges them via `client.sources.delete(notebook_id, source_id)` before uploads begin.
  - Automatically prunes purged records from `notebooklm_sync.json`.
- **`NotebookLMAuthError` Propagation**:
  - Detects session expiration during upload workers and stops further queue processing immediately.

### C. State Persistence (`backend/unisync/services/notebooklm/state.py`)
- Tracks file relative paths, SHA-256 hashes, byte sizes, and remote Google `source_id`s in `data/libraries/notebooklm_sync.json`.
- `is_source_synced(code, rel_path, file_hash)` ensures that unmodified files are skipped instantaneously (0ms overhead).

---

## 4. Verification & Citations

### A. FastMCP Query Verification
Query executed on COMP10001 via `unisync_notebooklm_query`:
> *"What are Lesson 29 and Lesson 30 about on EdStem?"*

**Ground Truth Citations Returned**:
- **Citation 1 (Score: 1.0)**: Source `c5103723-03ef-4ad4-9158-9ebfb7739935` (`EdStem Lesson 29 - Chess Problems (4 pts)`).
- **Citation 2 (Score: 0.999)**: Source `9cd5f728-76cd-428b-a1d1-65a5f4274e8a` (`EdStem Lesson 30 - CSC Problem (1 pt)`).
- Synthesis accurately described Worksheet 7: Review and the chess notation validation exercises.

### B. Live API Endpoints
- `GET /api/v1/notebooklm/status`:
  - `COMP10001`: `synced_sources_count: 163`, `is_provisioned: true`
  - `BLAW10001`: `synced_sources_count: 248`, `is_provisioned: true`
  - `INFO10003`: `synced_sources_count: 67`, `is_provisioned: true`
  - `CCDP10003`: `synced_sources_count: 17`, `is_provisioned: true`
- `GET /api/v1/notebooklm/courses`:
  - `COMP10001`: `total_sources: 163` (14 documents, 31 transcripts, 82 lessons, 17 pages, 19 forum digests)
  - `BLAW10001`: `total_sources: 248` (4 documents, 92 transcripts, 152 pages)
  - `INFO10003`: `total_sources: 67` (20 documents, 7 transcripts, 40 pages)
  - `CCDP10003`: `total_sources: 17` (7 transcripts, 10 pages)
