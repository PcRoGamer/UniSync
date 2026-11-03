# UniSync 🎓🔄

**Universal University Sync Platform for Echo360, Canvas, EdStem, and Email**

UniSync is a dedicated synchronization engine and suite of tools designed to seamlessly aggregate coursework, lectures, discussion threads, and academic deadlines across university platforms into a single, unified pipeline.

---

## 🏛️ The Four Pillars

### 1. 🎥 Echo360 Lecture Capture
- **Parallel Syllabus & Section Fetching**: Concurrently queries Echo360 course syllabi with bounded semaphores (`asyncio.Semaphore(5)`).
- **Referer Spoofing**: Bypasses Echo360's cross-origin protections natively without browser dual-mode hacks.
- **Direct-to-Disk Video Streaming**: Minimal memory footprint when downloading large lecture recordings (100MB–1GB+).
- **Subtitle & WebVTT Engine**: Automated subtitle extraction and transcript synchronization.
- **Theatre Mode Viewer**: Rich lecture player with timestamp-synced transcripts.

### 2. 📚 Canvas LMS
- **Module & Item Crawler**: Recursive retrieval of course modules, items, assignments, and files.
- **ETag Zero-Compute Caching**: Employs HTTP `If-None-Match` conditional requests to skip re-downloading unchanged course trees (`304 Not Modified`).
- **Concurrent Course Fetching**: Parallelized module downloads across enrolled subjects.
- **Direct File Streaming**: Streams large PDFs and slide decks straight to disk.

### 3. 💬 EdStem
- **Direct JSON API Integration**: Authenticated via personal API token (`https://edstem.org/api`) rather than fragile web scraping.
- **Lesson Content Linearizer**: Custom HTML-to-Markdown engine converting rich EdStem lesson content and code blocks.
- **Discussion Threads & Announcements**: Crawls threads, student questions, instructor replies, and course announcements.
- **Due Date Extraction**: Parses assignments, quizzes, and deadlines into normalized objects with stable URLs.

### 4. 📬 University Email Monitor (IMAP & Microsoft Graph)
- **Background Polling Loop**: Periodically polls university inboxes with customizable interval, sender filters, and subject filters.
- **Unread State Preservation**: Uses IMAP `BODY.PEEK[]` by default so messages remain marked unread in the user's mail client.
- **Microsoft Graph OAuth Flow**: Azure AD / Microsoft 365 OAuth 2.0 PKCE authentication for university accounts where basic IMAP is disabled.
- **Durable Ledger (`processed.jsonl`)**: Append-only log ensuring emails are never processed twice across process restarts.
- **LLM Deadline Extraction**: Automatically extracts assignment deadlines, exam dates, courses, and priorities from raw emails.
- **Todos Pipeline**: Dispatches structured deadlines directly into the academic Todos store.
- **24-Hour AI Digest**: Generates summarized digests of inbox activity over the last 24 hours.

---

## 📁 Repository Structure

```
UniSync/
├── README.md                          # Project documentation & architecture
├── pyproject.toml                     # Python package & dependency configuration
│
├── backend/
│   ├── unisync/
│   │   ├── __init__.py
│   │   ├── core/                      # Shared core utilities
│   │   │   ├── __init__.py
│   │   │   ├── base_sync.py           # Background sync service base class
│   │   │   ├── file_io.py             # Atomic file operations (crash-safe JSON/text)
│   │   │   └── path_service.py        # Workspace path resolution
│   │   ├── services/                  # Platform integration clients
│   │   │   ├── __init__.py
│   │   │   ├── sync/                  # Canvas & Echo360 API clients
│   │   │   │   ├── __init__.py
│   │   │   │   ├── canvas_client.py   # Canvas LMS HTTP client
│   │   │   │   └── echo360_client.py  # Echo360 HTTP client
│   │   │   ├── edstem/                # EdStem API client & snapshot engine
│   │   │   │   ├── __init__.py
│   │   │   │   └── client.py          # EdStemClient, Markdown parser, snapshot renderer
│   │   │   ├── email_monitor/         # Email monitor package
│   │   │   │   ├── __init__.py
│   │   │   │   ├── service.py         # Polling loop & worker
│   │   │   │   ├── graph_auth.py      # Azure AD / Microsoft Graph OAuth
│   │   │   │   ├── store.py           # Durable processed.jsonl ledger & cursor
│   │   │   │   ├── models.py          # Email & deadline Pydantic models
│   │   │   │   ├── extractor.py       # AI deadline extraction pipeline
│   │   │   │   ├── summarizer.py      # 24h AI email digest generator
│   │   │   │   └── connectors/        # IMAP, Graph, Mock, and MIME parsers
│   │   │   │       ├── base.py
│   │   │   │       ├── imap.py
│   │   │   │       ├── graph.py
│   │   │   │       ├── fake.py
│   │   │   │       └── parse_utils.py
│   │   │   ├── todos/                 # Academic deadlines & todos service
│   │   │   └── courses/               # Course registry
│   │   └── api/                       # FastAPI route handlers
│   │       ├── __init__.py
│   │       ├── main.py                # FastAPI app entry point & CORS
│   │       ├── sync_engine.py         # Delta diffing, Canvas/Echo sync routes
│   │       ├── sync_ws.py             # Push WebSocket progress broadcaster
│   │       ├── lectures.py            # Video playback & lecture syllabus API
│   │       ├── edstem.py              # EdStem routes (settings, test, import, courses)
│   │       ├── email_monitor.py       # Email monitor routes (test, poll, summarize)
│   │       └── todos.py               # Todos CRUD API
│   └── tests/                         # Test suites
│       ├── test_edstem.py             # EdStem integration tests (100% passing)
│       ├── test_email_monitor_lifespan.py
│       ├── test_email_monitor_router.py
│       └── services/
│           ├── email_monitor/
│           └── todos/
│
├── frontend/                          # Web UI & React components
│   ├── app/
│   │   ├── library/                   # Course discovery & sync UI
│   │   │   ├── page.tsx
│   │   │   └── [id]/page.tsx          # Theatre-mode video player & transcript viewer
│   │   ├── settings/
│   │   │   ├── edstem/page.tsx        # EdStem API token configuration
│   │   │   └── email/                 # IMAP / Graph email configuration
│   │   └── space/todos/               # Extracted todos view
│   ├── hooks/
│   │   └── useSyncProgress.ts         # Push WebSocket progress hook
│   └── lib/
│       ├── canvas-userscript.ts
│       ├── echo360-userscript.ts
│       ├── email-monitor-api.ts
│       ├── subtitle-utils.ts
│       └── todos-api.ts
│
├── userscript/                        # Browser extension / userscript
│   └── echo360-connector.user.js      # Dual-mode Echo360 / Canvas / EdStem browser bridge
│
├── docs/                              # Architecture specifications & handoff docs
│   ├── email-monitor.md               # Email monitor architecture & setup
│   ├── HANDOFF_CANVAS_AUTOSYNC.md     # Canvas autosync architecture
│   ├── SYNC_PIPELINE_PRE_P0.md        # Sync pipeline design & concurrency controls
│   └── SYNC_WEBSOCKET_DESIGN.md       # Push WebSocket progress protocol
│
└── reference/                         # Upstream reference implementations & payloads
    ├── scratch_echo360_payload.json
    ├── canvas-course-downloader/
    ├── canvas-downloader/
    ├── mohsen_echo360/
    ├── soraxas_echo360/
    └── tuyoik_echo360/
```

---

## 🗺️ Provenance: DeepTurtol Extraction Map

| Component | Source Path in DeepTurtol | UniSync Path | Status |
|---|---|---|---|
| **Echo360 Client** | `deeptutor/services/sync/echo360_client.py` | `backend/unisync/services/sync/echo360_client.py` | Extracted |
| **Echo360 Userscript** | `web/public/echo360-connector.user.js` | `userscript/echo360-connector.user.js` | Extracted |
| **Subtitle Utils** | `web/lib/subtitle-utils.ts` | `frontend/lib/subtitle-utils.ts` | Extracted |
| **Lecture API** | `deeptutor/api/routers/lectures.py` | `backend/unisync/api/lectures.py` | Extracted |
| **Theatre Mode UI** | `web/app/(workspace)/library/[id]/page.tsx` | `frontend/app/library/[id]/page.tsx` | Extracted |
| **Canvas Client** | `deeptutor/services/sync/canvas_client.py` | `backend/unisync/services/sync/canvas_client.py` | Extracted |
| **Sync Engine Router** | `deeptutor/api/routers/sync_engine.py` | `backend/unisync/api/sync_engine.py` | Extracted |
| **WebSocket Broadcaster** | `deeptutor/api/routers/sync_ws.py` | `backend/unisync/api/sync_ws.py` | Extracted |
| **Library Sync UI** | `web/app/(workspace)/library/page.tsx` | `frontend/app/library/page.tsx` | Extracted |
| **EdStem Client** | `7d27c9de:deeptutor/services/edstem.py` | `backend/unisync/services/edstem/client.py` | Restored from Git Snapshot |
| **EdStem Tests** | `7d27c9de:tests/services/test_edstem.py` | `backend/tests/test_edstem.py` | Restored & Verified |
| **EdStem Settings UI** | `7d27c9de:web/app/(utility)/settings/edstem/page.tsx` | `frontend/app/settings/edstem/page.tsx` | Restored from Git Snapshot |
| **EdStem API Router** | `7d27c9de:deeptutor/api/routers/settings.py` (EdStem subset) | `backend/unisync/api/edstem.py` | Extracted & Modularized |
| **Email Monitor Package** | `deeptutor/services/email_monitor/` | `backend/unisync/services/email_monitor/` | Extracted |
| **Email Monitor Router** | `deeptutor/api/routers/email_monitor.py` | `backend/unisync/api/email_monitor.py` | Extracted |
| **Email Settings UI** | `web/app/(utility)/settings/email/` | `frontend/app/settings/email/` | Extracted |
| **Todos Service & API** | `deeptutor/services/todos/` & `deeptutor/api/routers/todos.py` | `backend/unisync/services/todos/` & `backend/unisync/api/todos.py` | Extracted |
| **Core Utilities** | `deeptutor/services/file_io.py`, `base_sync.py` | `backend/unisync/core/` | Extracted |
| **Documentation** | `DOCS/email-monitor.md`, `SYNC_*.md`, `HANDOFF_*.md` | `docs/` | Extracted |

---

## 🚀 Quickstart

### Prerequisites
- Python 3.10+
- (Optional) Node.js 18+ for frontend UI

### 1. Install Backend Dependencies
```bash
cd backend
pip install -e ".[test]"
# Or:
pip install fastapi uvicorn httpx pydantic requests beautifulsoup4 python-multipart pytest pytest-asyncio
```

### 2. Run Tests
```bash
# Run EdStem test suite:
pytest backend/tests/test_edstem.py
```

### 3. Launch the API Server
```bash
python backend/unisync/api/main.py
# Or using uvicorn:
uvicorn unisync.api.main:app --host 127.0.0.1 --port 8000 --reload
```
API documentation will be available at `http://127.0.0.1:8000/docs`.
