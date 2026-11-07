# EdStem authenticated submissions and grading

This feature archives only the authenticated account owner's accessible EdStem
assessment records. A completed lesson is **not** proof of a submitted attempt,
and a submitted attempt is **not** proof of a released grade.

## What is archived

For EdStem `attempt` lessons with an authoritative `submitted_at`, the sync
retrieves the user's final submitted attempt and accessible marking status via
read-only endpoints. Published marks, rubric marks, final comments and the
user's own quiz responses are preserved when available.

If the grade is unreleased or an endpoint is unavailable, the status reflects
that limitation. UniSync never fabricates a score or marks a draft as submitted.

Records are written only to the per-user UniSync data root, under a course's
`edstem/submissions/` directory, and are excluded from Git via `.gitignore`.
The course sync summary reports verified counts and explicit error categories,
without logging access tokens or entire response bodies.

## Integration

`sync_edstem_course_full` builds a `grades_by_lesson` mapping from
`archive_edstem_assessments` before syncing tasks. This prevents unrelated
or merely completed EdStem lessons from being incorrectly marked graded.

The archive does **not** submit work, change marks, attempt restricted student
content, or download code-workspace originals that require separate APIs.

## Tests

From the repository:

```powershell
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_edstem.py backend/tests/test_edstem_submission_archive.py -q
```

The global pytest fixture isolates `UNISYNC_DATA_DIR` before tests load to
avoid mixing synthetic data with the user's real university records.
