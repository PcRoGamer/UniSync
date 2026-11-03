# Email Monitor — University Inbox → Structured Todos

The **email monitor** watches a university (Microsoft 365 / IMAP) inbox,
extracts assignments, exams, and deadlines using AI, and stores them as
structured todo items visible under **Space → Todos**.

## How it works

1. **IMAP connector** polls the configured mailbox at a regular interval.
   Uses `BODY.PEEK[]` by default so messages stay unread.
2. **Durable ledger** (`processed.jsonl`) prevents any message from being
   processed twice, even across restarts.
3. **LLM extraction** turns each email into structured JSON: title, due date,
   course, priority, and kind (assignment / exam / meeting / admin / other).
4. **Todo dedup** — same email + same title never creates duplicate todos
   (secondary guard alongside the ledger).

## Configuration

**Settings → Email** (`/settings/email`):

| Setting | Description |
|---------|-------------|
| Enable | Start/stop the background poll loop |
| Consent | Explicit opt-in for AI email processing |
| IMAP host/port | Defaults to `outlook.office365.com:993` |
| Username / Password | Full email address + app password (recommended) or IMAP password |
| Mailbox | Typically `INBOX` |
| Poll interval | Minimum 60 seconds |
| Sender filter | Glob pattern, e.g. `*@unimelb.edu.au` |
| Subject filter | Keyword match, e.g. `assignment` |
| Mark as seen | Whether to mark processed emails as read |
| Bootstrap last N | Process up to N historical messages on first enable (max 50) |
| Test Connection | Verify IMAP credentials without starting the monitor |

## Todos page

**Space → Todos** (`/space/todos`):

- **Filter tabs**: Upcoming, Open, Done, All
- **Inline editing**: title, due date, kind, priority, course, notes
- **Quick actions**: complete (circle toggle), dismiss, edit, delete
- **Overdue highlight**: red left-border + red date text
- **Email side panel**: monitor status + "Summarize last 24h" button

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/todos` | List with optional `?status=&kind=&due_before=&due_after=` |
| `POST` | `/api/v1/todos` | Create manual todo |
| `PATCH` | `/api/v1/todos/{id}` | Update / complete / dismiss |
| `DELETE` | `/api/v1/todos/{id}` | Delete |
| `GET` | `/api/v1/todos/upcoming` | Open items sorted by due date |
| `GET` | `/api/v1/email-monitor/settings` | Config (password masked) |
| `PUT` | `/api/v1/email-monitor/settings` | Save config |
| `POST` | `/api/v1/email-monitor/test` | Test IMAP login |
| `POST` | `/api/v1/email-monitor/poll-now` | Trigger one poll cycle |
| `GET` | `/api/v1/email-monitor/status` | Running state, poll count, last error |
| `POST` | `/api/v1/email-monitor/summarize` | Generate AI digest of recent emails |

## Storage

```
data/user/settings/email_monitor.json     — IMAP credentials + preferences
data/user/workspace/email_monitor/
  processed.jsonl                          — durable message ledger
  cursor.json                              — last UID / poll time
  summaries/                               — cached AI digests
data/user/workspace/todos/
  todos.json                               — canonical todo list
```

## Future

- **Microsoft Graph OAuth** — passwordless sign-in via Azure AD (connector stub
  exists at `deeptutor/services/email_monitor/connectors/graph.py`)
- **Scheduled summaries** — daily/weekly digest via existing `CronService`
- **Chat tool** — agent asks "check my inbox" mid-conversation
