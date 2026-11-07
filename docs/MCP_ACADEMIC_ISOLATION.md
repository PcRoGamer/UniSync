# Academic MCP course scoping, provenance, and private agent workspace

UniSync can expose academic records to agents over the **existing MCP tunnel**
without merging separate subjects, rewriting original documents, or creating
another network service.

## Course ownership

Every course-scoped search and read must verify source ownership against
enrolment metadata or a separately verified archived Echo360 section catalog.
An unknown or ambiguous course filter fails rather than returning all courses.

Archived sections are loaded only from the local, Git-ignored
`libraries/echo360_section_subjects.json` when its schema confirms that they
are **archived and unlinked**, not active Canvas enrolments. Course attribution
is based on explicit section UUID provenance, not transcript keywords.

A change in an indexed document's course ownership causes re-indexing even if
the source file's timestamp is unchanged. Local index paths can be rebased from
previous UniSync installations to the canonical data root.

## Shared agent workspace

The local `data/workspace/agent_shared/` directory holds private drafts,
handoffs, reports and notes between ChatGPT and local agents. MCP tools offer
paginated, scrubbed reads and SHA-256-guarded atomic writes with version history.
Writing a draft does **not** submit it to Canvas/EdStem or publish it anywhere.

The workspace is under the user's private, Git-ignored data directory. Do not
commit its contents or use it to store passwords, cookies or login exports.

## Retrieval and privacy

Academic retrieval is restricted to supported local academic roots. Response
egress applies credential and academic-boilerplate filtering before content
leaves the MCP tunnel. Original downloaded university files are unchanged by
the response scrubber. The MCP stream can start lazily on first protocol
activity; no second connection, LLM call, or desktop approval daemon is used.

## Verification

Run the isolated regression suites:

```powershell
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_agent_workspace.py backend/tests/test_mcp_security.py backend/tests/test_mcp.py backend/tests/test_mcp_course_scope.py backend/tests/test_kb_migration_paths.py -q
```

The synthetic course-scoping tests do not contain or require real learner
information, course account IDs, or Echo360 archive records.
