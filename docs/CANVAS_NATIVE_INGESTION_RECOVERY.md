# Canvas native crawl recovery and assessment catalog

This change makes UniSync's Canvas synchronization resilient to restricted or
inconsistent list endpoints while preserving source attribution and reported
completeness.

## Discovery and fallback

Canvas course pages are discovered from the normal authenticated pages API when
available. When listing is unavailable, the crawler can use accessible
module-linked page slugs, cached page references and official linked records.
It records how each page was found, which requests failed, and which items remain
unresolved. It does not claim a course is completely synced when discovery is
incomplete.

The sync discovers file IDs from modules, accessible pages, assignments,
announcements, discussions, and quizzes, plus the accessible file listing. An
existing manifest entry only counts as downloaded if its local file still exists
with the expected size. Otherwise UniSync retries the authorized download.

## Assessment links

The read-only assessment catalog records official Canvas assessment links and
metadata. Respondus-protected assessments remain links with clear restrictions.
UniSync **does not bypass Respondus, read restricted quiz questions, or submit
quiz attempts**.

Unreleased, inaccessible or unresolved records remain marked as such, not
silently treated as complete.

## Private data

Cached pages, source links, submission metadata and downloaded files live under
the user's Git-ignored UniSync data root. No university account data, cookies,
signed URLs or downloaded PDFs are included in this source change.

## Testing

```powershell
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_canvas_native_ingestion.py backend/tests/test_canvas_session_survival.py backend/tests/test_canvas_assessment_catalog.py backend/tests/test_submission_grade_only.py -q
```

Tests include missing/blocked list endpoints, mixed page discovery,
manifest/crawl completeness, source registration, and grade-only submission
archival. The pytest root is isolated to prevent mixing generated test records
with the live user's course data.
