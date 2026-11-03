# UniSync for macOS — Complete Setup & Agent Integration Guide

UniSync connects your university coursework (**Canvas LMS**, **Perusall**, **Echo360**, and **EdStem**) directly into your AI assistants (**Claude Desktop**, **Cursor**, **Codex**, or **Antigravity**) on macOS (Apple Silicon M1/M2/M3/M4 or Intel).

---

## ⚡ 1. Prerequisites (macOS)

UniSync requires **Python 3.10+**. Check your version in Terminal:
```bash
python3 --version
```
*If not installed, install it via Homebrew: `brew install python`.*

---

## 🚀 2. Installation & Quick Launch

1. **Extract the archive**:
   ```bash
   tar -xzf UniSync-macOS.tar.gz
   cd UniSync
   ```

2. **Start the local service**:
   ```bash
   ./start.sh
   ```
   *What this does automatically:*
   - Provisions a local `.venv` virtual environment.
   - Installs required dependencies (`fastapi`, `uvicorn`, `mcp`, `pypdf`, `python-docx`, `python-pptx`).
   - Starts the background UniSync backend on `http://127.0.0.1:8000/`.
   - Opens your default macOS browser (Safari or Chrome).

3. **Install Browser Extension (Tampermonkey)**:
   - Install the free [Tampermonkey Extension](https://www.tampermonkey.net/) for your browser.
   - Click **[Install Connector Script](http://127.0.0.1:8000/userscript/echo360-connector.user.js)** on your local dashboard.
   - Click **Install** in Tampermonkey.

4. **Sync Your Courses**:
   - Open your university **Canvas** tab (e.g. `https://canvas.lms.unimelb.edu.au/`) and log in.
   - UniSync will automatically discover your enrolled subjects, download your slides and rubrics, and sync assignment deadlines.

---

## 🧹 3. Does It Clean Files Already on Disk?

**Yes, through three automated defense layers:**

### Layer 1: Automatic Background On-Disk Scrubber (Autotriggered)
You never need to remember to run a cleanup script. The on-disk scrubber is **100% automated and autotriggered** across multiple lifecycle events:
* **Server Boot (`start.sh`)**: On startup, a non-blocking daemon thread immediately scans and scrubs any existing course files on disk.
* **MCP Server Startup (`run_mcp.sh`)**: When Claude Desktop, Cursor, or Codex connects, a background cleaner pass scrubs `data/courses/` without delaying agent startup or stdio communication.
* **Post-Sync Ingestion**: Whenever new Canvas items, EdStem lessons, or Echo360 lecture transcripts arrive, the scrubber automatically runs in the background.

*(Optional manual trigger)*: You can also manually trigger a batch scrub at any time:
```bash
./clean_disk.sh
# or target a specific course:
./clean_disk.sh data/courses/COMP10001
# or via REST API:
curl -X POST http://127.0.0.1:8000/api/v1/sync/clean-disk
```
*Scans all `.md` and `.txt` files on disk, strips institutional disclaimers, Turnitin notices, and GenAI declaration headers, and rewrites the files cleanly in-place.*

### Layer 2: Automatic Write-Time Sanitization (New Ingestions)
Whenever new course materials, lecture transcripts, or EdStem lesson slides are downloaded from your LMS, they are passed through `filter_academic_boilerplate()` before hitting the filesystem. New files arrive on disk 100% clean.

### Layer 3: Dynamic Read-Time Sanitization (MCP Gateway)
Even if an uncleaned raw file exists on disk, any read made by an AI agent through UniSync MCP tools (`unisync_read_course_file`, `unisync_search_course_materials`, `unisync_search`, `unisync_query_knowledge_base`) is dynamically filtered on the fly. The agent never sees policy disclaimers.

---

## 🤖 4. Connect to Your AI Assistant (MCP Setup)

UniSync includes a standalone launcher script (`run_mcp.sh`) that manages `PYTHONPATH` and runs the FastMCP server over standard JSON-RPC `stdio`.

### Option A: Claude Desktop (macOS)
Open `~/Library/Application Support/Claude/claude_desktop_config.json` in TextEdit or your editor:

```json
{
  "mcpServers": {
    "unisync": {
      "command": "/bin/bash",
      "args": ["/Users/YOUR_USERNAME/UniSync/run_mcp.sh"]
    }
  }
}
```
*(Replace `/Users/YOUR_USERNAME/UniSync` with the actual path where you extracted the package)*

---

### Option B: Cursor (macOS)
1. Open **Cursor Settings** (`Cmd + ,`) $\rightarrow$ **Features** $\rightarrow$ **MCP**.
2. Click **+ Add New MCP Server**:
   * **Name**: `unisync`
   * **Type**: `command`
   * **Command**: `/bin/bash /Users/YOUR_USERNAME/UniSync/run_mcp.sh`

---

### Option C: Codex / Antigravity
1. **MCP Server Configuration**:
   Add to your `~/.codex/config.toml` (or Antigravity MCP settings):
   ```toml
   [mcp_servers.unisync]
   command = "/bin/bash"
   args = ["/Users/YOUR_USERNAME/UniSync/run_mcp.sh"]
   startup_timeout_sec = 120
   tool_timeout_sec = 120
   enabled = true
   ```

2. **Installing the Codex Skill**:
   UniSync includes a complete, pre-configured Codex skill (`codex/skill/unisync-university`). Install it into your local skills directory:
   ```bash
   mkdir -p ~/.codex/skills
   cp -r codex/skill/unisync-university ~/.codex/skills/
   ```
   *For Antigravity, copy it into `~/.gemini/antigravity/skills/`:*
   ```bash
   mkdir -p ~/.gemini/antigravity/skills
   cp -r codex/skill/unisync-university ~/.gemini/antigravity/skills/unisync
   ```
   *Once installed, Codex and Antigravity will automatically activate UniSync whenever you ask coursework, assignment, rubric, or lecture questions!*

---

## 🛡️ 5. What Makes UniSync Superior to Raw `grep` / `glob`

* **Exact Code & Substring Grep**: `unisync_search_course_materials(query="def foo", search_content=True)` searches inside code and documents, returning exact line numbers and clean excerpts in 1–2 seconds.
* **Full-Text PDF & Slide Extraction**: Automatically parses `.pdf`, `.pptx`, `.docx`, and `.vtt` lecture transcripts.
* **Context Window Hygiene**: Completely excises repetitive university policies and GenAI warning banners, preventing AI hesitancy or false-positive refusals.
* **Cross-Silo Correlation**: Unifies Canvas deadlines, EdStem discussions, Echo360 video timestamps, and Google NotebookLM in a single call.
