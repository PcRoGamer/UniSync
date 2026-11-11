# Canvas submission capture

UniSync now archives the signed-in student's Canvas submissions after successful
Camoufox login or session refresh, and when native sync starts. Expired refreshes
automatically open interactive Camoufox login; no legacy browser bridge is needed.

Manual trigger: `POST /api/v1/sync/submissions`, optionally with `?course_id=237296`.
Progress and errors: `GET /api/v1/sync/submissions/status`.

The same request also archives Gradescope work linked from each Canvas course. See
[Gradescope](#gradescope) below. The native scheduler runs this submission archive
after each full native sync.

The service paginates active student courses and their assignments, then reads
`submissions/self` with submission history, regular and HTML comments, and rubric
assessment fields. This restricts capture to the current student's submission
records, including group-assignment records Canvas makes available to that
student. It never submits work or changes Canvas records.

Each attempt is stored under
`data/courses/<course_code>/submissions/<assignment_id>/attempt_<number>/`:

- `submission.json`: submitted text and URL, score and grade timestamps, instructor
  comments, rubric assessment feedback, attachment paths and errors.
- A uniquely named Markdown summary with the submission and feedback, for existing
  course search and reading tools.
- Attachments named with course, assignment, attempt and Canvas file IDs.

Downloads stream through temporary files and are renamed only on success. Empty,
incomplete and HTML login responses are rejected. Existing downloads are reused;
failed downloads are retried on the next sync. Historical attempts remain separate.
Canvas JSON requests now share the resident browser cookie jar. Streamed file
requests select cookies for each URL and return response cookie changes to the
browser, including across CDN redirects.
Signed attachment URLs are not persisted in submission metadata or error messages.

The existing incremental KB index runs after capture, making PDF, DOCX, PPTX, TXT
and Markdown files searchable. Other formats (including ZIP and video) are archived
without extracting or transcribing them. Submitted external URLs are preserved,
not crawled. Submission and rubric comment HTML is reduced to readable text, and
comment attachment URLs are not persisted. Media feedback is recorded as an available
media comment, but its audio/video content is not downloaded. This feature does not
upload submissions to NotebookLM. A group's submission must be visible through the
student's Canvas account to be captured.

Validation: mocked Canvas tests cover pagination, own-submission retrieval, group
provenance, attempt history, URL/text capture, repeat sync, cookie isolation,
safe filenames, failed downloads, HTML/size rejection, and automatic SSO fallback.

Canvas API reference:
https://developerdocs.instructure.com/services/canvas/resources/submissions

## Gradescope

Gradescope is read through the Canvas "Gradescope" course tab. UniSync does not
store a Gradescope password. The resident Camoufox session follows the normal
student LTI launch (Canvas, `canvas.instructure.com`, `sso.canvaslms.com`, then
`www.gradescope.com`) and only keeps that student's submission pages. Roster
tables and other courses are not downloaded. Requests are paced, and failures
are recorded as a host, path, and status or a short message. Cookies, form
fields, and query strings are not written to logs or sync status.

Each saved submission lands in
`data/courses/<course_code>/gradescope/<gradescope_course>/<assignment>/<submission>/`:

- `graded.pdf`: the marked submission, kept only when the file starts with `%PDF-`.
- `submission.json`: total score, per-question scores, applied rubric items, and comments.
- `feedback.md`: the same feedback in Markdown. The normal course index walks
  `courses/**/*.md`, so this summary is searchable after sync.

Unchanged submissions (same submission id, updated time, and content hash, with
a valid PDF already on disk) are skipped. A regrade replaces the PDF and rewrites
the JSON and Markdown. When the Gradescope title matches one Canvas assignment,
`submission.json` records that Canvas assignment id.

`GET /api/v1/sync/submissions/status` includes a `gradescope` object keyed by
Canvas course id. `no_gradescope_tab` means that course has no Gradescope tab.
An `error` string is the safe failure message for that course.

MCP: `unisync_get_assignment_detail` adds a `gradescope` object when a local
archive matches the Canvas assignment. `unisync_get_gradescope_feedback(course,
assignment)` reads the same archive. Both responses go through the MCP scrubber.

### One-course check on the owner's machine

This needs a Camoufox window already signed in to Canvas. COMP10001 (Canvas
course `241246`) is the sample course; its Gradescope tool is
`/courses/241246/external_tools/5945`. The mid-semester test submission used as
the fixture model is Gradescope course `1375834`, assignment `8511948`,
submission `422550001`, and Canvas assignment `661924`.

1. `POST /api/v1/sync/submissions?course_id=241246`
2. Poll `GET /api/v1/sync/submissions/status` until `state` is `done`.
3. Under `gradescope`, course `241246` should show `downloaded` or `cached`
   counts. An `error` value should name the host, path, and status, not a cookie
   or token.
4. Open `data/courses/<COMP10001 course_code>/gradescope/1375834/8511948/422550001/`.
   `graded.pdf` starts with `%PDF-`. `submission.json` has the total score,
   question scores, applied rubric items, and comments. `feedback.md` is beside it.
5. Ask the MCP tools for `unisync_get_gradescope_feedback` with course
   `COMP10001` and assignment `Mid-semester Test`, and
   `unisync_get_assignment_detail` for Canvas assignment `661924`. The Gradescope
   score and question comments should appear, and the payload should not contain
   launch tokens.

Run the same POST again. A second pass with no regrade should increment `cached`
rather than downloading the PDF again.
