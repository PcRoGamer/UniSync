# Echo360 recording links in the downloads manifest

Each Echo360 subtitle row in `data/downloads_manifest.json` carries the transcript download URL in `direct_link` and the watchable recording page in `recording_url`. `lesson_id` is the Echo360 lesson id when one is known. `metadata.recording_url_source` says which evidence produced the link:

| `recording_url_source` | Evidence |
| --- | --- |
| `syllabus` | Lesson id from `data/cache/echo360_syllabus.json`, or the lesson id the Echo360 section sync just registered |
| `media_library` | `source_url` already stored on `data/media_library/<id>/metadata.json` |
| `embed_catalog` | `player_url` / `provider_launch_url` on `data/libraries/echo360_embeds.json`, or `https://echo360.net.au/ui/player/<resolved_media_id>` when that is the id the sync resolved |
| `filename` | Fallback for `G_` subtitle filenames only. `:` in the lesson id was saved as `_` |

Constructed classroom links use the watch URL the lecture catalog already emits:

`https://echo360.net.au/lesson/<lesson_id>/classroom?focus=Video`

A `source_url` copied from the media library is kept as stored. Embed rows use the player URL the sync resolved (`/lti/...`, `/ui/player/...`, or `/lesson/.../classroom`). `https://echo360.net.au/media/<id>/public` is not used. Only `https` URLs on `echo360.net.au` (or a subdomain) are kept. Echo360 lesson URLs in this repo have no start-time parameter, so cue timestamps stay next to `recording_url` and are not appended to it.

`unisync_search_lectures`, `unisync_get_lecture_transcript`, `unisync_search` lecture hits, and Echo360 rows from `unisync_search_course_materials` return the same `recording_url`. Those fields pass through `backend/unisync/mcp/egress.py`, which clears a recording link that is not an Echo360 watch page and leaves the URL in place when it is.

## Regenerate on the normal pipeline

No separate backfill. The next run that writes the manifest fills `recording_url` on existing Echo360 rows and on rows it registers in that same run.

1. Start UniSync the usual way (`python backend/unisync/api/main.py` or the repo's uvicorn command) and let the normal Echo360 sync run. Section payloads posted to `POST /api/v1/sync/payload` with `data.type` `echo360_sections` register each cached subtitle, including `lesson_id`, and flush `data/downloads_manifest.json`.
2. Loading the download registry does the same fill for files already on disk. That load happens on the first inventory or dashboard request (`GET /api/v1/sync/inventory/list`) after startup, and again inside every manifest flush. Subtitles already listed gain `recording_url` without editing the JSON by hand.

After that run, for an Echo360 subtitle entry check:

- `direct_link` still points at the `.vtt` transcript download.
- `recording_url` is an `https://echo360.net.au/...` lesson, player, or LTI page.
- `lesson_id` is set when a lesson or embed id was resolved.
- `metadata.recording_url_source` is one of `syllabus`, `media_library`, `embed_catalog`, or `filename`.

`POST /api/v1/sync/inventory/refresh-markdown` rewrites `DOWNLOADS_INVENTORY.md` from the same registry. Echo360 rows with a recording link show it beside the transcript link.
