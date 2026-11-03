# Camoufox-native Canvas ingestion

The Canvas sync is authenticated by the resident Camoufox profile. It does not
depend on the legacy userscript/browser bridge for routine Canvas material
collection. The profile, its cookies and browser storage stay owned by
`CamoufoxService`; snapshots and download ledgers never contain credentials.

## Retained Canvas coverage

For each active Canvas course, the native sync retrieves and stores:

- course overview and syllabus;
- modules and file-linked module items;
- assignment details, submissions, descriptions and full rubric criteria/rating
  tiers (the individual assignment endpoint is used when the collection result
  omits a rubric);
- wiki pages with hydrated page bodies;
- navigation tabs, including the course's Lecture Capture/Echo360 launch URL;
- Echo360 links embedded in Canvas pages, assignments and module items, added
  to the shared Echo360 catalog; and
- announcements, discussions and classic-quiz instructions when Canvas exposes
  them through its API; and
- files linked from assignments, modules and pages.

Course material is rendered deterministically into
`data/courses/<knowledge-base>/pages/canvas-<course-id>-*.md`. Assignment
snapshots include a Markdown rubric table. Assignment records are also updated
in Todos; a partial Canvas response cannot erase a previously saved full rubric.
The prior `auto_sync: false` course setting is respected during a full sync;
requesting a targeted refresh explicitly refreshes that course.

## Attachments and indexing

Attachment discovery matches the previous bridge's sources: assignment
descriptions/attachments, module file items and page file links. Every discovered
file is processed in batches of three, with each response streamed to disk. Each
download follows Canvas's short-lived redirect through `CanvasSessionTransport`,
which reads current browser cookies for the Canvas hop and never forwards them
to a third-party CDN.

Completed files are recorded in `data/libraries/canvas_files.json` only after a
successful write, making later runs incremental. The KB index is updated after
the course refresh. Files and snapshots remain on disk if indexing is busy and
will be picked up by the next index pass.

## Targeted refresh

`POST /api/v1/sync/courses/{course_id}/refresh` refreshes a single enrolled
course through the same resident session. This is intended for a newly released
brief or rubric and avoids an all-course refresh. If a full sync is already
running, the endpoint returns `already_syncing` rather than creating a second
browser workload.
