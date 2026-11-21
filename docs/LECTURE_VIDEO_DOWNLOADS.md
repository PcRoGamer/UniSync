# Lecture video downloads

UniSync saves Echo360 transcripts on every sync. Video files stay off until you ask for them. A normal sync does not pull multi-gigabyte recordings.

Downloaded files go to `data/lecture_videos/<course>/<lesson>.mp4` (or `.ts` when ffmpeg is not installed and the stream is HLS). `data/downloads_manifest.json` records `video_status`, `video_filepath`, `video_bytes`, and `video_quality` on the lecture row. Lecture MCP lookups return those fields. Signed playlist URLs, cookies, and access-grant parameters are not stored or returned.

The default rendition is the tallest one at or under 720 pixels, and at most 1.5 GiB (`UNISYNC_LECTURE_VIDEO_MAX_HEIGHT`, `UNISYNC_LECTURE_VIDEO_MAX_BYTES`). A file that is already on disk is skipped. A partial `.part` file for a progressive MP4 is resumed. Set the size cap lower if you want the SD file when both exist.

## One Echo360-course lecture

This uses the lesson and media id already stored in `data/cache/echo360_syllabus.json`, then the Echo360 session in the UniSync browser profile.

```bash
PYTHONPATH=backend python -m unisync.services.sync.lecture_videos --course COMP10001 --limit 1
```

A specific date or title:

```bash
PYTHONPATH=backend python -m unisync.services.sync.lecture_videos --course COMP10001 --lecture 2026-07-28
```

## One BLAW module-page lecture

BLAW recordings are Echo360 players embedded in Canvas. `/lti/` and `/ui/player/` links do not play on their own. The downloader opens the Canvas launch URL already stored on the embed (`data/libraries/echo360_embeds.json`) with the Canvas session, then downloads the stream that launch reveals.

```bash
PYTHONPATH=backend python -m unisync.services.sync.lecture_videos --course BLAW10001 --limit 1
```

Or one title:

```bash
PYTHONPATH=backend python -m unisync.services.sync.lecture_videos --course BLAW10001 --lecture "divisible contracts"
```

The same calls exist as the MCP tool `unisync_download_lecture_videos(course, lecture, limit)`. `unisync_search_lectures` and `unisync_get_lecture_transcript` include `video_downloaded` and `video_filepath` after a file is saved.

## Opt in during sync

```bash
UNISYNC_DOWNLOAD_LECTURE_VIDEOS=1
```

With that set, an Echo360 sync that already has a resolved media URL downloads it through the browser session. Without the variable, that sync still saves subtitles and leaves the video row as `video_opt_in`.

Run this from the machine that already has the UniSync browser profile signed in to Canvas and Echo360. This environment cannot log in to those sites.

On a live run, check three things if a file does not appear:

* Echo360 course lectures need the syllabus cache and a signed-in Echo360 session. Player-properties is tried first. If that returns 401, sign in again through UniSync's browser profile.
* BLAW lectures need `canvas_launch_url` on the embed. The downloader opens that URL in the same browser session transcript ingest uses. A module page that never starts the player will finish as `unavailable` with `lti_unresolved` or `media_id_unresolved`.
* Some Echo360 tenants only offer HLS. Without ffmpeg the file is a concatenated `.ts`. With ffmpeg it is remuxed to `.mp4`. A progressive `hd1`/`sd1` file is used when the CDN actually has one and it is under the size cap.

The download path follows the public Echo360 layout (player-properties, the classroom page, and progressive files beside an HLS playlist). [echo360-downloader](https://github.com/mohsen-ameli/echo360-downloader) has no license, and [EchoPlus](https://github.com/zinfector/EchoPlus) names ISC in `package.json` without a license file, so none of their code is copied. Neither extension performs a Canvas LTI launch; BLAW uses UniSync's existing browser session instead.
