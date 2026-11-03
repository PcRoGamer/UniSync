# Archive: Browser Extension & Userscript Bridge (DEPRECATED)

> **Status: DEPRECATED & ARCHIVED**  
> **Active Replacement: Camoufox Stealth Browser Service (`unisync.services.browser.camoufox_service`)**

---

## 1. Overview & Context

This directory archives the legacy **UniSync Browser Extension & Userscript Bridge** (v3.x - v4.0.3), originally built as a Tampermonkey userscript (`echo360-connector.user.js`) and dynamic backend bridge runtime (`bridge-runtime.js`).

The userscript operated by injecting into active browser tabs across:
- `canvas.lms.unimelb.edu.au` (Canvas LMS)
- `echo360.net.au` (Lecture Capture)
- `edstem.org` (Discussion & Lessons)
- `outlook.office.com` (Student Email)

It communicated with UniSync's local backend server at `http://127.0.0.1:8000` via `GM_xmlhttpRequest` and `postMessage` leader election.

---

## 2. Why the Browser Extension was Deprecated

While functional for basic HTML scraping, the browser extension / userscript model suffered from severe structural limitations:

1. **High Setup Friction for Students**:
   - Required installing third-party browser extensions (Tampermonkey / Violentmonkey).
   - In modern Chrome (138+), users had to manually enable "Developer Mode" and grant "Allow User Scripts" permissions.
   - Required students to keep active university tabs open in their everyday personal browsers.

2. **Cross-Origin IFrame Sandboxing**:
   - Lecture recordings and EdStem lessons are frequently embedded inside Canvas pages via LTI iframes (e.g., `canvas.lms.unimelb.edu.au/courses/.../external_tools/4090`).
   - Browser extensions are blocked by cross-origin security policies from inspecting, intercepting, or extracting video streams and authenticated session tokens inside these nested iframes.

3. **Session Invalidation & Lack of Autonomous Sync**:
   - When browser cookies expired, the extension had no way to self-heal or execute background synchronization without manual student navigation.
   - AI agents (like Antigravity or DeepTutor) operating via MCP could not trigger syncs on demand if the user was not at their desk with a browser open.

---

## 3. The Active Replacement: Camoufox Stealth Browser

UniSync now uses **Camoufox** (`backend/unisync/services/browser/camoufox_service.py`), a C++ engine-level anti-detect Firefox automation engine:

| Feature | Legacy Userscript / Extension | Camoufox Stealth Browser (`camo`) |
| :--- | :--- | :--- |
| **User Installation** | Requires Tampermonkey & manual script install | **Zero install** (bundled with Python environment) |
| **Authentication** | Dependent on existing daily browser tab | **1-Click native Okta SSO window** (`interactive_login`) |
| **Session Persistence** | Volatile per tab session | **Persistent profile** in `data/browser_profile/camoufox/` |
| **Autonomous Refresh** | Cannot run headlessly | **Headless background refresh** (`background_sync_session`) |
| **LTI Iframe Penetration** | Blocked by browser sandbox | **Native network request interception** (`resolve_echo360_embed`) |
| **Bot Detection Bypass** | Easily flagged by Cloudflare / Akamai | **C++ engine-level anti-detect** stealth spoofing |

---

## 4. Archived File Inventory

* `echo360-connector.user.js`: The bootstrap loader userscript that ran in Tampermonkey.
* `bridge-runtime.js`: The dynamic JavaScript payload fetched by the loader to perform Canvas API calls and Echo360 DOM scraping.

---

## 5. Backward Compatibility

To maintain compatibility with existing tests and any legacy endpoints, the FastAPI router in `backend/unisync/api/dashboard.py` and `backend/unisync/api/lectures.py` serves deprecation notices and headers (`Deprecation: true`, FastAPI `deprecated=True`). All new integrations must use `CamoufoxService` and UniSync MCP tools.
