# BLAW10001 Echo360 discovery milestone

Date: 2026-09-10 (Australia/Melbourne)

## Confirmed working

- The backend-served userscript is now a thin loader. Its runtime can be updated from the backend without reinstalling the userscript.
- Canvas course `240718` is crawled automatically through its module items; the Canvas pages listing endpoint itself returns HTTP 404 for this course.
- The crawl built an inventory of 137 Canvas page items and selected 45 likely lecture/video pages.
- The page `1-dot-1-how-pbl-works` was fetched successfully (HTTP 200), yielded 2 Echo360 links, and was persisted through `/api/v1/sync/payload` (HTTP 200).
- The crawl has persisted 41 BLAW-related Echo360 embed records so far.
- Backend bridge command and result endpoints are available for remote diagnostics and targeted browser probes.
- Dashboard/runtime verification currently passes 9 tests; the runtime passes Node syntax checking.

## Not yet confirmed

- A newly downloaded `.vtt` from the currently open BLAW Echo360 player has not yet been verified.
- The visible Echo360 player returns a 404 for the guessed public media URL and previously returned 403 for guessed transcript-download endpoints. These are not treated as proof that captions are unavailable; the authenticated rendered player track URL still needs to be captured.
- A lecture media download has not yet been verified from the newly discovered BLAW embeds.

## Next acquisition path

1. Use the authenticated Echo360 player DOM to capture the rendered `<track src>` or transcript-download URL.
2. Ingest and validate the response as `WEBVTT`, recording status and byte count in backend bridge results.
3. If no caption track is mounted, inspect the authenticated player bootstrap/media configuration for an HLS (`.m3u8`) or MP4 URL and persist the lecture media instead.
4. Keep all probes backend-triggered so the user does not need to reinstall or manually open each Canvas page.

