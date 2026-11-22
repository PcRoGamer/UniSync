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

## Two-feed lectures and the picture-in-picture file

Many Echo360 lectures have two video feeds: the screen capture and a camera on the room. When both exist the downloader saves:

* `<lesson>.screen.mp4` - the screen feed with the lecture audio
* `<lesson>.camera.mp4` - the camera feed with the lecture audio
* `<lesson>.pip.mp4` - the screen full frame with the camera as a small circle in a corner

The manifest row and MCP lookups add `video_screen_filepath`, `video_camera_filepath`, `video_pip_filepath`, `video_pip_encoder`, `video_pip_crop` and `video_feeds`. `video_filepath` points at the picture-in-picture file when it was built.

Echo360-course lessons that only open inside Canvas are listed from the course's Canvas Lecture Capture page (`/courses/<id>/external_tools/701`, or `lecture_capture_url` on the library entry, or `UNISYNC_LECTURE_CAPTURE_URL` with `{cid}`). The downloader asks Echo360 for the lesson's files from inside that frame. It doesn't play anything there.

Every page the downloader opens is muted. The browser profile sets `media.volume_scale=0`, and a script mutes every audio and video element in every frame before it can play. A file shorter than 2 minutes (`UNISYNC_LECTURE_VIDEO_MIN_SECONDS`) is treated as a pre-roll disclaimer. It is set aside as `.short.mp4` and the row is marked `suspicious_short`.

| Setting | Default | What it does |
|---|---|---|
| `UNISYNC_LECTURE_VIDEO_PIP` | on | `0` skips the combined file |
| `UNISYNC_LECTURE_VIDEO_PIP_SIZE` | `0.22` | circle diameter as a share of the screen width |
| `UNISYNC_LECTURE_VIDEO_PIP_POSITION` | `bottom-right` | or `bottom-left`, `top-right`, `top-left` |
| `UNISYNC_LECTURE_VIDEO_PIP_MARGIN` | `0.02` | gap to the frame edge, share of the width |
| `UNISYNC_LECTURE_VIDEO_PIP_ENCODER` | `auto` | a working hardware H.264 encoder, else `libx264` |
| `UNISYNC_LECTURE_VIDEO_PIP_CRF` / `_PRESET` | `23` / `veryfast` | libx264 quality |
| `UNISYNC_LECTURE_VIDEO_FFMPEG_THREADS` | `2` | decoder, filter and encoder threads |

ffmpeg runs at below-normal priority on Windows (`nice 10` elsewhere).

### Presenter tracking for the camera circle

The camera is usually a wide shot of the room, so a fixed centre crop can show an empty lectern. With tracking on, the camera feed is sampled every 2 seconds at 416 px wide. People are found with NanoDet-Plus, a 3.8 MB COCO detector from the [OpenCV model zoo](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_nanodet) (Apache-2.0), on one CPU thread through OpenCV's DNN module. There's no GPU and no cloud service.

* The circle follows whoever is presenting at the front, guest speakers included. It doesn't lock onto one person.
* Audience heads in the bottom rows and small boxes are ignored.
* Someone who appears for less than 4 seconds, such as a student walking past, is never followed.
* If the presenter is briefly not detected (turned away, walked behind the lectern), the last framing is held for 10 seconds. After that the circle falls back to the fixed crop.
* Several presenters widen the circle to the full camera height to fit them. If they are too far apart even for that, the fixed crop is used.
* The crop centre eases toward the target with a speed cap and a dead zone, so it doesn't jitter. ffmpeg applies the plan with `sendcmd` on the crop during the single encode pass.

Tracking adds about one minute of CPU for a one-hour lecture (one decode pass of the camera feed plus about 1,650 detections).

| Setting | Default | What it does |
|---|---|---|
| `UNISYNC_LECTURE_VIDEO_PIP_TRACK` | on when OpenCV is installed | `0` turns tracking off |
| `UNISYNC_LECTURE_VIDEO_PIP_CROP` | `center` | fixed crop when tracking is off or unsure: `center`, `left`, `right`, or the centre as a fraction (`0.3`) |
| `UNISYNC_LECTURE_VIDEO_PIP_ZOOM` | `0.6` | tracked crop height as a share of the camera height (`1` = no zoom) |
| `UNISYNC_LECTURE_VIDEO_PIP_TRACK_INTERVAL` | `2` | seconds between samples |
| `UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_SECONDS` | `4` | how long someone must be visible before the circle follows them |
| `UNISYNC_LECTURE_VIDEO_PIP_TRACK_IGNORE_BELOW` | `0.6` | ignore people whose box starts below this share of the frame height |
| `UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_HEIGHT` | `0.15` | ignore boxes shorter than this share of the frame height |
| `UNISYNC_LECTURE_VIDEO_PIP_MODEL` | `<data>/models/object_detection_nanodet_2022nov.onnx` | model file |
| `UNISYNC_LECTURE_VIDEO_PIP_MODEL_DOWNLOAD` | on | `0` never fetches the model; without it tracking falls back to the fixed crop |

Tracking is optional. Install it with:

```bash
pip install "unisync[video-tracking]"   # or: pip install opencv-python-headless
```

Without OpenCV, or when the model can't be fetched, the picture-in-picture file uses the fixed crop. `video_pip_crop` on the row says which was used, for example `tracked (followed 98% of samples)` or `fixed center (tracking needs opencv-python-headless)`.