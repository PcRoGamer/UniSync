# UniSync Master Project Index & Inventory 🗂️

**Generated**: `2026-09-04T23:29:21.537871+00:00`
**Project Root**: [`c:/Users/Admin/Desktop/UniSync`](file:///c:/Users/Admin/Desktop/UniSync)

---

## 📁 Codebase Component Index

| Pillar / Category | File / Module | Direct Link | Original DeepTurtol Source | Purpose & Details |
|---|---|---|---|---|
| **Echo360** | `echo360_client.py` | [`echo360_client.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/sync/echo360_client.py) | `deeptutor/services/sync/echo360_client.py` | Core Echo360 API client with parallel syllabus queries, referer spoofing, direct media streaming |
| **Echo360** | `echo360-connector.user.js` | [`echo360-connector.user.js`](file:///c:/Users/Admin/Desktop/UniSync/userscript/echo360-connector.user.js) | `web/public/echo360-connector.user.js` | Dual-mode userscript connecting Echo360, Canvas, and EdStem to backend |
| **Echo360** | `lectures.py` | [`lectures.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/api/lectures.py) | `deeptutor/api/routers/lectures.py` | Video playback, thumbnail generation, lecture syllabus endpoints |
| **Echo360** | `echo360-userscript.ts` | [`echo360-userscript.ts`](file:///c:/Users/Admin/Desktop/UniSync/frontend/lib/echo360-userscript.ts) | `web/lib/echo360-userscript.ts` | Frontend userscript bridge & session listener |
| **Echo360** | `subtitle-utils.ts` | [`subtitle-utils.ts`](file:///c:/Users/Admin/Desktop/UniSync/frontend/lib/subtitle-utils.ts) | `web/lib/subtitle-utils.ts` | Subtitle parser, WebVTT cleaner, timestamp sync |
| **Echo360** | Theatre Player | [`[id]/page.tsx`](file:///c:/Users/Admin/Desktop/UniSync/frontend/app/library/[id]/page.tsx) | `web/app/(workspace)/library/[id]/page.tsx` | Theatre-mode lecture video player with synchronized interactive transcript |
| **Canvas** | `canvas_client.py` | [`canvas_client.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/sync/canvas_client.py) | `deeptutor/services/sync/canvas_client.py` | Canvas LMS API client with ETag caching, concurrent module crawler |
| **Canvas** | `canvas-userscript.ts` | [`canvas-userscript.ts`](file:///c:/Users/Admin/Desktop/UniSync/frontend/lib/canvas-userscript.ts) | `web/lib/canvas-userscript.ts` | Canvas session and token injector for client-side scraper |
| **Canvas** | `sync_engine.py` | [`sync_engine.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/api/sync_engine.py) | `deeptutor/api/routers/sync_engine.py` | Zero-compute delta sync engine, course diffing, file manifest |
| **Canvas** | Library UI | [`library/page.tsx`](file:///c:/Users/Admin/Desktop/UniSync/frontend/app/library/page.tsx) | `web/app/(workspace)/library/page.tsx` | Course discovery dashboard, sync progress indicators, card views |
| **EdStem** | `client.py` | [`client.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/edstem/client.py) | `7d27c9de:deeptutor/services/edstem.py` | Token-authenticated JSON API client, Markdown parser, lesson/thread snapshot builder |
| **EdStem** | `test_edstem.py` | [`test_edstem.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/tests/test_edstem.py) | `7d27c9de:tests/services/test_edstem.py` | 5/5 passing unit tests validating auth, error handling, snapshot rendering |
| **EdStem** | `edstem.py` API | [`edstem.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/api/edstem.py) | `7d27c9de:deeptutor/api/routers/settings.py` | FastAPI router for EdStem settings, test connection, courses, snapshot import |
| **EdStem** | Settings UI | [`edstem/page.tsx`](file:///c:/Users/Admin/Desktop/UniSync/frontend/app/settings/edstem/page.tsx) | `7d27c9de:web/app/(utility)/settings/edstem/page.tsx` | API token configuration, test connection button, course picker |
| **Email** | `service.py` | [`service.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/service.py) | `deeptutor/services/email_monitor/service.py` | Background email polling loop, filter rules, cursor management |
| **Email** | `graph_auth.py` | [`graph_auth.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/graph_auth.py) | `deeptutor/services/email_monitor/graph_auth.py` | Microsoft 365 Azure AD OAuth PKCE authentication |
| **Email** | `store.py` | [`store.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/store.py) | `deeptutor/services/email_monitor/store.py` | Durable `processed.jsonl` ledger ensuring emails are never processed twice |
| **Email** | `models.py` | [`models.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/models.py) | `deeptutor/services/email_monitor/models.py` | Pydantic models for EmailMessage, ExtractedTodo, EmailMonitorSettings |
| **Email** | `extractor.py` | [`extractor.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/extractor.py) | `deeptutor/services/email_monitor/extractor.py` | LLM pipeline parsing assignments, due dates, exams, and priority |
| **Email** | `summarizer.py` | [`summarizer.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/summarizer.py) | `deeptutor/services/email_monitor/summarizer.py` | 24-hour summary digest generator for inbox |
| **Email** | `connectors/imap.py` | [`imap.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/services/email_monitor/connectors/imap.py) | `deeptutor/services/email_monitor/connectors/imap.py` | IMAP client with SSL & `BODY.PEEK[]` preserving unread status |
| **Email** | `email_monitor.py` | [`email_monitor.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/api/email_monitor.py) | `deeptutor/api/routers/email_monitor.py` | Email monitor API routes (status, test, poll-now, summarize) |
| **Email** | Settings UI | [`email/page.tsx`](file:///c:/Users/Admin/Desktop/UniSync/frontend/app/settings/email/page.tsx) | `web/app/(utility)/settings/email/page.tsx` | Email monitor settings & credentials UI |
| **Shared Sync** | `sync_ws.py` | [`sync_ws.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/api/sync_ws.py) | `deeptutor/api/routers/sync_ws.py` | Singleton `SyncProgressBroadcaster` fanning out WebSocket progress events |
| **Shared Sync** | `useSyncProgress.ts` | [`useSyncProgress.ts`](file:///c:/Users/Admin/Desktop/UniSync/frontend/hooks/useSyncProgress.ts) | `web/hooks/useSyncProgress.ts` | Push WebSocket hook with automatic HTTP polling fallback |
| **Shared Sync** | `base_sync.py` | [`base_sync.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/core/base_sync.py) | `deeptutor/services/base_sync.py` | Base periodic background sync service |
| **Shared Sync** | `file_io.py` | [`file_io.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/core/file_io.py) | `deeptutor/services/file_io.py` | Atomic crash-safe file writes with exponential backoff on Windows file locks |
| **Shared Sync** | `download_registry.py` | [`download_registry.py`](file:///c:/Users/Admin/Desktop/UniSync/backend/unisync/core/download_registry.py) | **NEW in UniSync** | Unified registry tracking direct links and local filepaths for all downloads |
| **Documentation** | `DOWNLOADS_INVENTORY.md` | [`DOWNLOADS_INVENTORY.md`](file:///c:/Users/Admin/Desktop/UniSync/DOWNLOADS_INVENTORY.md) | **NEW in UniSync** | 390+ item index with direct links, sizes, and filepaths |
| **Documentation** | `email-monitor.md` | [`email-monitor.md`](file:///c:/Users/Admin/Desktop/UniSync/docs/email-monitor.md) | `DOCS/email-monitor.md` | Full email monitor architecture, setup guide, and ledger storage |
| **Documentation** | `HANDOFF_CANVAS_AUTOSYNC.md` | [`HANDOFF_CANVAS_AUTOSYNC.md`](file:///c:/Users/Admin/Desktop/UniSync/docs/HANDOFF_CANVAS_AUTOSYNC.md) | `DOCS/HANDOFF_CANVAS_AUTOSYNC.md` | Canvas autosync pipeline handoff notes |
| **Documentation** | `SYNC_PIPELINE_PRE_P0.md` | [`SYNC_PIPELINE_PRE_P0.md`](file:///c:/Users/Admin/Desktop/UniSync/docs/SYNC_PIPELINE_PRE_P0.md) | `DOCS/SYNC_PIPELINE_PRE_P0.md` | Concurrency controls and zero-compute diffing specs |
| **Documentation** | `SYNC_WEBSOCKET_DESIGN.md` | [`SYNC_WEBSOCKET_DESIGN.md`](file:///c:/Users/Admin/Desktop/UniSync/docs/SYNC_WEBSOCKET_DESIGN.md) | `DOCS/SYNC_WEBSOCKET_DESIGN.md` | Push WebSocket progress protocol design |
