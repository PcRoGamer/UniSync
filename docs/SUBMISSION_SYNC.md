# Canvas submission capture

UniSync now archives the signed-in student's Canvas submissions after successful
Camoufox login or session refresh, and when native sync starts. Expired refreshes
automatically open interactive Camoufox login; no legacy browser bridge is needed.

Manual trigger: `POST /api/v1/sync/submissions`, optionally with `?course_id=237296`.
Progress and errors: `GET /api/v1/sync/submissions/status`.

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
