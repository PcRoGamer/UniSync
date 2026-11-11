# Echo360 recording links in the downloads manifest

Each Echo360 subtitle row in `data/downloads_manifest.json` carries the transcript download URL in `direct_link` and the watchable recording page in `recording_url`. `lesson_id` is the Echo360 lesson id when one is known. `metadata.recording_url_source` says which evidence produced the link:

| `recording_url_source` | Evidence |
| --- | --- |
| `syllabus` | Lesson id from `data/cache/echo360_syllabus.json`, or the lesson id the Echo360 section sync just registered |
| `media_library` | `source_url` already stored on `data/media_library/<id>/metadata.json` |
| `embed_catalog` | A stored public page on `data/libraries/echo360_embeds.json`, or a Canvas page / module item / Lecture Capture URL the catalog already recorded |
| `filename` | Fallback for `G_` subtitle filenames only. `:` in the lesson id was saved as `_` |

`recording_url_kind` says what the link does:

| `recording_url_kind` | Meaning |
| --- | --- |
| `echo360_lesson` | `https://echo360.net.au/lesson/<lesson_id>/classroom?focus=Video` |
| `echo360_public` | `https://echo360.net.au/media/<id>/public` or `/public/media/<id>` |
| `canvas_embed_page` | A Canvas page, module item, or Lecture Capture tool URL (`canvas.lms.unimelb.edu.au`) already stored on the embed or course library. It opens inside Canvas. |
| `unplayable_lti` | The catalog only has `/lti/` or `/ui/player/`, which do not play on their own. `recording_url` is left empty. No Canvas URL is invented. |

Constructed classroom links use the watch URL the lecture catalog already emits:

`https://echo360.net.au/lesson/<lesson_id>/classroom?focus=Video`

A lesson link needs an Echo360 session. The first click can land on `https://login.echo360.net.au` when the browser is only signed in through Canvas. A public media page plays without that redirect. `/lti/<id>` renders blank outside Canvas's LTI launch, and `/ui/player/<id>` returns 403 on its own, so those are not stored as `recording_url`. The shared Canvas placement UUID is never treated as a media id.

Query strings keep only `focus`, `linkId`, `autoplay`, `automute`, `display`, `currentMediaId`, and a nested `url`. `sessionId`, `secureLinkAccessDataId`, and other access-grant parameters are removed when the manifest is written and again in MCP egress. Echo360 lesson URLs in this repo have no start-time parameter, so cue timestamps stay next to `recording_url` and are not appended to it.

`unisync_search_lectures`, `unisync_get_lecture_transcript`, `unisync_search` lecture hits, and Echo360 rows from `unisync_search_course_materials` return `recording_url` and `recording_url_kind`. Those fields pass through `backend/unisync/mcp/egress.py`. Egress keeps Echo360 lesson and public pages, and keeps a Canvas page or Lecture Capture URL only on `recording_url`.

## Regenerate on the normal pipeline

No separate backfill. The next run that writes the manifest fills `recording_url` on existing Echo360 rows and on rows it registers in that same run.

1. Start UniSync the usual way (`python backend/unisync/api/main.py` or the repo's uvicorn command) and let the normal Echo360 sync run. Section payloads posted to `POST /api/v1/sync/payload` with `data.type` `echo360_sections` register each cached subtitle, including `lesson_id`, and flush `data/downloads_manifest.json`.
2. Loading the download registry does the same fill for files already on disk. That load happens on the first inventory or dashboard request (`GET /api/v1/sync/inventory/list`) after startup, and again inside every manifest flush. Subtitles already listed gain `recording_url` without editing the JSON by hand.

After that run, for an Echo360 subtitle entry check:

- `direct_link` still points at the `.vtt` transcript download.
- `recording_url` is an Echo360 lesson page, an Echo360 public media page, or a Canvas page / Lecture Capture URL. It is empty when the only known player is an LTI or `/ui/player/` link.
- `recording_url_kind` is `echo360_lesson`, `echo360_public`, `canvas_embed_page`, or `unplayable_lti`.
- `lesson_id` is set when a lesson or embed id was resolved.
- `metadata.recording_url_source` is one of `syllabus`, `media_library`, `embed_catalog`, or `filename`.

`POST /api/v1/sync/inventory/refresh-markdown` rewrites `DOWNLOADS_INVENTORY.md` from the same registry. Echo360 rows with a recording link show it beside the transcript link.
