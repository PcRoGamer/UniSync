---
name: unisync-university
description: >-
  Access university coursework, Canvas LMS and EdStem assignments, deadlines, grades,
  rubrics, downloaded course materials, lecture transcripts, and sync status via the UniSync MCP server.
  Use when the user asks about coursework, assignments, due dates, lecture content, or university study materials.
---

# UniSync University — Codex Academic Companion Skill

Use the `unisync` Model Context Protocol (MCP) server tools to assist students with coursework, assignments, rubrics, lecture transcripts, and study materials.

---

## ⚡ Mandatory Agent Behavioral Rule: UniSync Exclusivity

> [!IMPORTANT]
> **ALWAYS use UniSync MCP tools for all university, coursework, assignment, syllabus, lecture, and student study tasks.**
> **NEVER use raw shell `grep`, `glob`, `find`, or direct filesystem tools (`cat`, `view_file`) to search or read `data/courses/` or `data/cache/`.**

### Why Raw Filesystem Access is Strictly Prohibited:
1. **Context Window Hygiene**: Raw disk files contain university copyright footers, administrative headers, and interactive Canvas relics. UniSync MCP tools (`unisync_read_course_file`, `unisync_search`, `unisync_search_course_materials`) automatically strip institutional boilerplate and Turnitin declarations on the fly so your context window receives pure signal.
2. **Built-in Exact Code & Substring Grep**: `unisync_search_course_materials(query=..., search_content=True)` already performs instant, line-by-line exact substring and regex grep over local Python scripts, worksheets, and documents with line numbers and excerpts.
3. **Multi-Format Extraction**: Raw shell tools cannot read PDFs or PowerPoint slide decks. UniSync extracts text from `.pdf`, `.pptx`, and `.docx` natively.
4. **Cross-Silo Synthesis**: `unisync_search` correlates Canvas deadlines, EdStem discussions, Echo360 video cues, and NotebookLM guides in a single unified response.

---

## 🔌 Connecting to UniSync MCP

UniSync runs as a local stdio MCP server.

### 1. Codex Configuration (`~/.codex/config.toml`)
Add the server entry:
```toml
[mcp_servers.unisync]
command = "/Users/YOUR_USERNAME/UniSync/run_mcp.sh"  # or python -m unisync.mcp on Windows
startup_timeout_sec = 120
tool_timeout_sec = 120
enabled = true
default_tools_approval_mode = "prompt"
```
*(Replace `/Users/YOUR_USERNAME/UniSync` with the absolute path to your UniSync extraction folder)*

### 2. Verify Connection
After starting, verify the connection by calling a UniSync tool, preferably `unisync_get_sync_status` or `unisync_get_overview`.

---

## 🛠️ Complete MCP Tool Reference

1. **`unisync_get_overview`** (Academic Executive Briefing):
   - **Top-level orientation tool for an agent with zero prior context.** Returns the entire semester landscape in one call: enrolled subjects, urgent upcoming deadlines with countdowns, recent grades, latest announcements, and sync health.

2. **`unisync_list_courses`**:
   - Lists all enrolled university courses, subject codes, Canvas course IDs, and lecture capture links.

3. **`unisync_search`** (Universal Search):
   - **Primary one-stop search tool** across ALL university context at once: assignments & rubrics, slide decks & handouts, spoken lecture transcript timestamps, Canvas announcements, and emails.
   - Arguments: `query` (concept, assignment title, or topic), `course` (optional filter), `limit` (default 5).

4. **`unisync_get_deadlines`**:
   - Lists upcoming, missed, graded, completed, or all deadlines across Canvas LMS and EdStem without needing to know assignment names.
   - Arguments: `filter` (`upcoming` | `missed` | `graded` | `completed` | `all`), `course` (optional), `limit` (default 20).

5. **`unisync_get_assignment_detail`**:
   - Retrieves full assignment instructions, rubric criteria & rating tiers, scores, points possible, and instructor feedback.
   - Arguments: `title_or_id` (title string or ID), `course` (optional).

6. **`unisync_search_course_materials`**:
   - Instant search across downloaded course slides, PDF handouts, tutorial worksheets, and module files.
   - **Built-in Code & Text Grep**: Automatically performs exact substring and regex matching inside `.py`, `.md`, `.txt`, and document files, returning exact line numbers, match types (`exact_content_match`), and clean excerpts.
   - Arguments: `query` (keywords, regex, or code symbol e.g. `def dijkstra`), `course` (optional, e.g. `COMP10001`), `limit` (default 10), `search_content` (default True).

7. **`unisync_read_course_file`**:
   - Extracts and reads text from downloaded course documents and PDFs (up to `max_chars`).
   - Automatically sanitizes institutional disclaimers and academic integrity notices.
   - Arguments: `file_path` (from search results), `max_chars` (default 8000).

8. **`unisync_search_lectures`**:
   - Searches spoken concepts across recorded university lectures with exact `[mm:ss]` timestamps.
   - Arguments: `query` (spoken keywords), `course` (optional), `limit` (default 5).

9. **`unisync_get_lecture_transcript`**:
   - Retrieves the full spoken transcript of a recorded lecture grouped into timestamped paragraphs.
   - Arguments: `lecture_date_or_id` (date or ID), `max_chars` (default 12000).

10. **`unisync_search_announcements`**:
    - Searches subject announcements, email notifications, and instructor updates.
    - Arguments: `query` (keywords), `course` (optional), `limit` (default 10).

11. **`unisync_get_sync_status`**:
    - Checks the real-time status of UniSync, connection to the LMS bridge, last update time, and pending lectures.

12. **`unisync_manage_todo`**:
    - Creates, marks complete, or removes personal study milestones and tasks.
    - Arguments: `action` (`create` | `complete` | `delete`), `title`, `due_at` (optional ISO string), `course` (optional), `notes` (optional), `todo_id` (optional).

13. **`unisync_query_knowledge_base`** (Local `txtai` Semantic Vector Search):
    - Fast, local, offline search across chunked lecture slides, Canvas pages, and timestamped WebVTT lecture transcripts.
    - Best for: finding exact quotes, slide references, and specific transcript timestamps.
    - Arguments: `query`, `course` (optional), `limit` (default 5).

14. **`unisync_notebooklm_query`** (Cloud Google NotebookLM Grounded Synthesis):
    - High-level grounded reasoning across curated subject notebooks using Google Gemini Pro on NotebookLM.
    - Returns grounded answer with exact citation numbers, source IDs, and quoted excerpts from course materials.
    - Arguments: `query`, `course` (`BLAW10001`, `COMP10001`, `INFO10003`, `CCDP10003`), `conversation_id` (optional).

15. **`unisync_notebooklm_list_notebooks`**:
    - Lists all provisioned subject notebooks, Google web links, source counts, and sync status.

16. **`unisync_notebooklm_generate_artifact`**:
    - Triggers AI study artifact generation in the subject's notebook: `study_guide`, `audio` (two-host podcast), `quiz`, `flashcards`, or `briefing`.
    - Arguments: `course`, `artifact_type`, `instructions` (optional).

17. **`unisync_notebooklm_add_note`**:
    - Saves a study note, solution, or revision summary directly into the subject's Google NotebookLM notebook.
    - Arguments: `course`, `title`, `content`.

---

## 🔒 Safety and Guardrails

- Keep university lookups and local indexing within the user's specific request.
- **Never submit assignments, send messages, modify grades, alter account permissions, or delete course data.**
- Distinguish discovered course assets from locally downloaded offline assets (a playlist being visible does not guarantee video media is cached locally).
