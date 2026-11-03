# UniSync — Quickstart & Setup Guide

UniSync connects your university courses (**Canvas LMS**, **Perusall**, **Echo360**, and **EdStem**) directly into your AI coding assistant (**ChatGPT Codex Desktop**, **Claude Desktop**, **Cursor**, or **Antigravity**).

Instead of pasting assignment sheets or hallucinating rubrics, your AI gets **instant, 100% accurate ground-truth** of your exact slides, readings, word count constraints, deadlines, and criteria.

---

## ⚡ 3-Minute Setup

### Step 1: Launch UniSync
- **Windows**: Double-click **`start.bat`** (or open terminal in this folder and run `python -m uvicorn unisync.api.main:app --port 8000`).
- **Mac / Linux**: Open Terminal in this folder and run `./start.sh`.

*The dashboard will automatically open at `http://127.0.0.1:8000/`.*

---

### Step 2: Install the Browser Bridge
UniSync syncs through your browser's existing university login—no passwords or API tokens required.

1. Install the free **Tampermonkey** extension for your browser:
   - [Chrome / Brave Web Store](https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   - [Firefox Add-ons](https://addons.mozilla.org/en-US/firefox/addon/tampermonkey/)
   - [Microsoft Edge Add-ons](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)
2. Once installed, open this link in your browser:
   👉 **[http://127.0.0.1:8000/userscript/echo360-connector.user.js](http://127.0.0.1:8000/userscript/echo360-connector.user.js)**
3. Click **Install** in Tampermonkey.

---

### Step 3: Trigger Your First Course Sync
1. Open your university **Canvas LMS** tab (e.g. `https://canvas.lms.unimelb.edu.au/`).
2. Log in normally. The Tampermonkey script will automatically:
   - Detect all your enrolled subjects.
   - Download your course slides, handouts, and assignment rubrics.
   - Synchronize your assignment due dates into your local calendar.
3. **If your course uses Perusall**: Opening [Perusall](https://app.perusall.com/) will link your reading assignments and documents.
4. **If your course uses Echo360**: Launching Echo360 once via your Canvas course's "Lecture Capture" link establishes the video transcript bridge.

---

### Step 4: Connect to Your AI Assistant

#### Option A: OpenAI Codex Desktop
Add the UniSync MCP server to your Codex configuration at `~/.codex/config.toml`:
```toml
[mcp_servers.unisync]
command = "FULL_PATH_TO_UNISYNC/run_mcp.sh"  # or python -m unisync.mcp.server on Windows
startup_timeout_sec = 120
tool_timeout_sec = 120
enabled = true
default_tools_approval_mode = "prompt"
```
*(A ready-to-use template is included at `codex/config/mcp-unisync.toml`)*

**Codex Skill (Optional)**: Copy `codex/skill/unisync-university` into `~/.codex/skills/` (or extract `codex/skill/unisync-university-skill.zip`) so Codex automatically recognizes when to query your courses and lecture materials.

#### Option B: Claude Desktop
Open Settings → Developer → Edit Config (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "unisync": {
      "command": "python",
      "args": ["-m", "unisync.mcp.server"],
      "env": {
        "PYTHONPATH": "FULL_PATH_TO_UNISYNC/backend"
      }
    }
  }
}
```

> **Tip for Mac / Linux**: You can also point `command` directly to the bundled `run_mcp.sh` script (e.g. `"/Users/yourname/UniSync/run_mcp.sh"`), which automatically sets `PYTHONPATH` and executes the virtual environment.

---

## 🎯 What to Ask Your AI

Once connected, your AI assistant has access to specialized university tools:

- **"Catch me up on my semester"**
  Calls `unisync_get_overview` $\rightarrow$ gives a complete briefing of all your subjects, next urgent deadlines, and recent announcements.

- **"What assignments are due this week?"**
  Calls `unisync_get_deadlines` $\rightarrow$ shows exact dates, countdowns, and points.

- **"Find all mentions of word count in my slides"**
  Calls `unisync_search` $\rightarrow$ searches across all PDF slides, Word docs, lecture transcripts, and Canvas pages simultaneously.

- **"Show me the rubric for Assignment 2"**
  Calls `unisync_get_assignment_detail` $\rightarrow$ gives the exact scoring criteria, rating tiers, and instructions.

---

## 🔒 Privacy & Local Security
- **100% Local**: All your course materials and notes stay on your machine in the `data/` folder.
- **Zero Cloud Costs**: Uses offline document parsing and local hybrid vector search.
- **No Passwords**: Authentication piggybacks on your browser's existing Canvas session. No credentials are ever sent to third parties.
