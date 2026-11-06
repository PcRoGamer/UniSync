# University email: capture once, retrieve on demand

UniSync is the authoritative archive for university email. Guardian and other
MCP clients should **search and read original messages when a task requires them**,
rather than asking an LLM to inspect every email in the background.

## Data flow

```
University mailbox (IMAP / Thunderbird / browser bridge)
    → deterministic fetch + MIME parsing (no model calls)
    → archive: message.eml when supplied, body.txt, body.html,
      metadata.json and actual attachment files when supplied
    → processed.jsonl durable ledger + IMAP/Thunderbird cursor
    → incremental SQLite FTS5 index (created on first archive search)
    → UniSync MCP: search → full-message read → attachment read
    → Guardian reasoning on demand, using retrieved source evidence
```

Capturing and indexing do not need Luna Low, model routing, embeddings,
summaries, task generation, or any other inference. The user may request
analysis **after** retrieving a message. University mail content is untrusted
input and must never be interpreted as instructions to the agent.

## Read-only MCP tools

| Tool | Input | Output |
|---|---|---|
| `unisync_search_emails` | `query`, optional `sender`, `since`, `until`, `limit` (max 50), `offset` | Stable email IDs, titles, sender, date, evidence snippets, attachment counts, `total_matched`, `next_offset` |
| `unisync_get_email` | `email_id`, `offset`, `max_chars`, `format=text\|eml` | Original archived email text and headers; paginated `body`, `has_more`, `next_offset`, attachment metadata |
| `unisync_read_email_attachment` | `email_id`, `attachment_index`, `offset`, `max_chars` | Text-readable archived attachments (text, PDF, DOCX, PPTX), or explicit unsupported-binary status |
| `unisync_get_email_sync_status` | No arguments | Connection/monitor state, indexed archive count and last error/poll |

The existing `unisync_search` universal tool also includes an `emails`
category. New tools **do not** expose arbitrary filesystem paths or content
outside the configured email archive, and are read-only.

Example retrieval loop:

1. Call `unisync_search_emails(query="exam room", limit=20)`.
2. Select a relevant `email_id` based on date, sender, and subject.
3. Call `unisync_get_email(email_id=..., max_chars=12000)`.
4. If `has_more`, follow `next_offset` until all required content is read.
5. If an attachment is relevant, use its numeric index with
   `unisync_read_email_attachment`.

No LLM calls occur during steps 1–5. The model may interpret results once it
has the original evidence.

## Durable capture and gaps

- `processed.jsonl` records successfully archived emails; failed records are
  logged but remain retryable.
- IMAP or Thunderbird cursors advance **only after successful archive
  completion**, so failed disk writes cannot silently skip email.
- IMAP UIDVALIDITY changes reset the UID range; failed UID fetches raise
  rather than allowing the cursor to pass them.
- MIME attachment filenames are sanitized and duplicates get unique names.
- The search index consumes **all** successfully archived ledger entries,
  not just the most recent 250. It follows complete appended JSONL lines and
  rebuilds if a ledger is replaced/truncated.
- A healthy UniSync MCP connection does **not** mean mailbox authentication
  is working. An empty archive is not proof that the mailbox is empty.
- IMAP's configured `mailbox` setting currently covers **one folder at a
  time**. Multi-folder completeness, historical coverage, Graph and
  Thunderbird connection health must still be verified after deployment.
- Attachments are readable only if their bytes were actually archived.
  Provider metadata alone is **not** a completed attachment download.

## Existing backend endpoints

`GET /api/v1/email-monitor/settings` returns a masked configuration;
`PUT /api/v1/email-monitor/settings` updates it.
`POST /api/v1/email-monitor/test` checks IMAP credentials,
`POST /api/v1/email-monitor/poll-now` starts a foreground poll,
`GET /api/v1/email-monitor/status` returns in-process statistics.

The historical setting `consent_granted` remains a sync-consent gate for
the polling service; it does **not** authorize sending message contents
to an LLM. Keep these existing HTTP endpoints separate from the new
read-only MCP retrieval tools.

## Security

Never pass `.env`, arbitrary local paths, or mailbox credentials through MCP.
Use stable source IDs; resolve their archive paths **inside UniSync**.
Original emails can contain tracking links, unsafe attachments, forged
headers, and prompt injection. Display these as evidence, not commands.
Keep raw mailbox data and SQLite FTS indexes private on the local machine.

## Deployment checklist

This implementation is in draft PR #1 until reconciled with the **newer
running UniSync checkout**. Do not overwrite the live `server.py` with
the older GitHub copy.

- Merge the four new MCP tools and the FTS5 archive module into the live
  server without losing existing pagination, redaction, or course features.
- Run email-monitor tests, archive durability tests, MCP registration tests,
  and verify all existing 21 tool names still work.
- Restart the live UniSync MCP service, discover its new tools, and check
  email sync health.
- Compare local indexed archive count with independent mailbox coverage;
  verify older records and recent messages, and read a real attachment.
- Then let Guardian use UniSync's retrieved records instead of running its
  own per-email AI classification. Keep any legacy Guardian email state
  until migration validation is complete.
