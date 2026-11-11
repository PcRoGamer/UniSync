"""Recording links are written with the downloads manifest and returned by lecture tools."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pytest

from unisync.core.download_registry import DownloadRegistry
from unisync.mcp.egress import scrub_lecture_payload, scrub_recording_url
from unisync.services.sync.echo360_ingest import direct_lesson_stems, embed_lesson_id, lesson_stem
from unisync.services.sync.echo360_links import (
    classroom_url,
    is_echo360_https_url,
    is_watch_page,
    lesson_id_from_filename,
    resolve_recording_link,
)
from unisync.services.sync import echo360_links

COMP_STEM = (
    "G_065eec77-13b6-455c-a044-9111057cc197_"
    "5645c5ef-f457-430f-939a-6415f263ba6a_"
    "2026-07-30T16_05_00.000_2026-07-30T17_00_00.000"
)
API_LESSON = (
    "G_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa_"
    "5645c5ef-f457-430f-939a-6415f263ba6a"
)
WINDOW_START = "2026-07-30T16:05:00.000"
WINDOW_END = "2026-07-30T17:00:00.000"
TOOL_UUID = "9381284d-5253-4e5b-b173-fb47a63576cb"
LINK_ID = "de5554c9-cdf5-4ff4-98b2-f9b156ebe79f"
MEDIA_ID = "79f0af68-f485-4775-b0df-9c551acd52ec"
VTT = "WEBVTT\n\n00:01:02.000 --> 00:01:05.000\ndijkstra finds the shortest path\n"


class _DataRoot:
    def __init__(self, root: Path) -> None:
        self._root = root

    def get_user_root(self) -> Path:
        return self._root


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "cache" / "echo360_subtitles").mkdir(parents=True)
    (root / "libraries").mkdir()
    (root / "media_library").mkdir()
    echo360_links._json_cache.clear()
    monkeypatch.setattr(
        "unisync.core.path_service.get_path_service",
        lambda: _DataRoot(root),
    )
    return root


def _write_vtt(root: Path, stem: str, body: str = VTT) -> Path:
    path = root / "cache" / "echo360_subtitles" / f"{stem}.vtt"
    path.write_text(body, encoding="utf-8")
    return path


def _write_syllabus(root: Path, lesson_id: str, start: str, end: str) -> None:
    payload = [{
        "sectionId": "5645c5ef-f457-430f-939a-6415f263ba6a",
        "syllabus": [{
            "lesson": {
                "lesson": {
                    "id": lesson_id,
                    "name": "COMP10001 Lecture",
                    "timing": {"start": start, "end": end},
                },
                "medias": [{"id": "media-1"}],
            }
        }],
    }]
    (root / "cache" / "echo360_syllabus.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    echo360_links._json_cache.clear()


def _manifest_item(root: Path, stem: str) -> dict:
    reg = DownloadRegistry(root / "downloads_manifest.json")
    saved = json.loads((root / "downloads_manifest.json").read_text(encoding="utf-8"))
    matches = [
        item for item in saved["items"].values()
        if item.get("platform") == "echo360" and stem in str(item.get("local_filepath"))
    ]
    assert len(matches) == 1
    assert len(reg.list_all(platform="echo360")) >= 1
    return matches[0]


def test_host_validation_accepts_only_https_echo360() -> None:
    good = classroom_url(lesson_id_from_filename(COMP_STEM))
    assert good.startswith("https://echo360.net.au/lesson/")
    assert is_echo360_https_url(good)
    assert is_watch_page(good)
    assert is_echo360_https_url("https://content.echo360.net.au/inst/1/s1_av.m3u8")
    assert is_watch_page("https://content.echo360.net.au/inst/1/s1_av.m3u8") is False

    rejected = [
        "http://echo360.net.au/lesson/abc/classroom",
        "https://echo360.net.au.evil.com/lesson/abc/classroom",
        "https://evil.example/lesson/abc/classroom",
        "https://user:secret@echo360.net.au/lesson/abc/classroom",
        "https://echo360.net.au/media/abc/public",
        "https://echo360.net.au/section/abc/home",
    ]
    for url in rejected:
        assert is_watch_page(url) is False, url
    assert scrub_recording_url(good) == good
    assert scrub_recording_url(rejected[0]) == ""
    assert scrub_recording_url(rejected[2]) == ""


def test_syllabus_lesson_id_is_preferred_over_the_filename(data_root: Path) -> None:
    item = {
        "lesson": {
            "lesson": {
                "id": API_LESSON,
                "timing": {"start": WINDOW_START, "end": WINDOW_END},
            }
        }
    }
    stems = direct_lesson_stems(item)
    timed = [stem for stem in stems if stem != lesson_stem(API_LESSON)]
    assert timed, stems
    _write_vtt(data_root, timed[0])
    _write_syllabus(data_root, API_LESSON, WINDOW_START, WINDOW_END)
    # A stored media URL must not override the syllabus lesson id.
    media_dir = data_root / "media_library" / "item1"
    media_dir.mkdir()
    (media_dir / "metadata.json").write_text(json.dumps({
        "source": "echo360",
        "source_id": lesson_id_from_filename(timed[0]),
        "source_url": "https://echo360.net.au/ui/player/79f0af68-f485-4775-b0df-9c551acd52ec",
        "media_file": "media.mp4",
    }), encoding="utf-8")

    row = _manifest_item(data_root, timed[0])
    assert row["recording_url"] == classroom_url(API_LESSON)
    assert row["lesson_id"] == API_LESSON
    assert row["metadata"]["recording_url_source"] == "syllabus"
    assert API_LESSON in row["recording_url"]
    assert "T16:05:00.000" not in row["lesson_id"]
    assert "transcript.vtt" in row["direct_link"]
    assert parse_qs(urlparse(row["recording_url"]).query) == {"focus": ["Video"]}


def test_filename_fallback_restores_colons_for_g_lessons(data_root: Path) -> None:
    _write_vtt(data_root, COMP_STEM)
    row = _manifest_item(data_root, COMP_STEM)
    lesson_id = lesson_id_from_filename(COMP_STEM)
    assert row["recording_url"] == classroom_url(lesson_id)
    assert row["lesson_id"] == lesson_id
    assert row["metadata"]["recording_url_source"] == "filename"
    assert "T16:05:00.000" in row["recording_url"]
    assert "T16_05_00.000" not in row["recording_url"]


def test_existing_manifest_row_gains_recording_url_on_load(data_root: Path) -> None:
    vtt = _write_vtt(data_root, COMP_STEM)
    manifest = data_root / "downloads_manifest.json"
    item_id = f"{COMP_STEM}:subtitles"
    key = f"echo360:{item_id}"
    manifest.write_text(json.dumps({
        "updated_at": "2026-01-01T00:00:00+00:00",
        "items": {
            key: {
                "key": key,
                "platform": "echo360",
                "item_id": item_id,
                "title": "COMP10001 lecture — subtitles.vtt",
                "direct_link": "https://echo360.net.au/media/download/old/transcript.vtt?lessonId=old",
                "local_filepath": str(vtt.resolve()),
                "course_id": "5645c5ef-f457-430f-939a-6415f263ba6a",
                "course_name": "COMP10001",
                "size_bytes": vtt.stat().st_size,
                "content_type": "text/vtt",
                "downloaded_at": "2026-01-01T00:00:00+00:00",
                "metadata": {"asset_type": "subtitle"},
            }
        },
    }), encoding="utf-8")

    DownloadRegistry(manifest)
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    row = saved["items"][key]
    assert row["recording_url"] == classroom_url(lesson_id_from_filename(COMP_STEM))
    assert row["metadata"]["recording_url_source"] == "filename"
    assert row["downloaded_at"] == "2026-01-01T00:00:00+00:00"
    assert row["direct_link"].endswith("lessonId=old")
    assert len(saved["items"]) == 1


def test_register_uses_the_lesson_id_from_the_sync_payload(data_root: Path) -> None:
    """The Echo360 section handler passes the API lesson id on the subtitle row."""
    lesson_id = lesson_id_from_filename(COMP_STEM)
    vtt = _write_vtt(data_root, COMP_STEM)
    reg = DownloadRegistry(data_root / "downloads_manifest.json")
    reg.register(
        platform="echo360",
        item_id=f"{lesson_id}:subtitles",
        title="COMP10001 — subtitles.vtt",
        direct_link=(
            f"https://echo360.net.au/media/download/{lesson_id}/transcript.vtt"
            f"?lessonId={lesson_id}"
        ),
        local_filepath=vtt,
        course_id="5645c5ef-f457-430f-939a-6415f263ba6a",
        course_name="COMP10001",
        content_type="text/vtt",
        metadata={
            "asset_type": "subtitle",
            "lesson_id": lesson_id,
            "source_url": "https://echo360.net.au/section/5645c5ef-f457-430f-939a-6415f263ba6a/home",
        },
    )
    saved = json.loads((data_root / "downloads_manifest.json").read_text(encoding="utf-8"))
    row = saved["items"][f"echo360:{lesson_id}:subtitles"]
    assert row["recording_url"] == classroom_url(lesson_id)
    assert row["lesson_id"] == lesson_id
    assert row["metadata"]["recording_url_source"] == "syllabus"
    assert "/section/" not in row["recording_url"]


def test_media_library_source_url_is_kept_when_syllabus_is_absent(data_root: Path) -> None:
    lesson_id = lesson_id_from_filename(COMP_STEM)
    stored = f"https://echo360.net.au/lesson/{lesson_id}/classroom"
    _write_vtt(data_root, COMP_STEM)
    media_dir = data_root / "media_library" / "clip"
    media_dir.mkdir()
    (media_dir / "metadata.json").write_text(json.dumps({
        "source": "echo360",
        "source_id": lesson_id,
        "source_url": stored,
        "media_file": "media.mp4",
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, COMP_STEM)
    assert row["recording_url"] == stored
    assert row["metadata"]["recording_url_source"] == "media_library"
    assert "focus" not in parse_qs(urlparse(row["recording_url"]).query)


def test_embed_stores_the_resolved_player_url(data_root: Path) -> None:
    lesson = embed_lesson_id(TOOL_UUID, LINK_ID)
    player = f"https://echo360.net.au/lti/{TOOL_UUID}?linkId={LINK_ID}"
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": TOOL_UUID,
            "media_id": TOOL_UUID,
            "link_id": LINK_ID,
            "player_url": player,
            "resolved_media_id": MEDIA_ID,
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row["recording_url"] == ""
    assert row["recording_url_kind"] == "unplayable_lti"
    assert row["lesson_id"] == lesson
    assert row["metadata"]["recording_url_source"] == "embed_catalog"
    assert "lti" not in row["recording_url"]


def test_embed_public_media_url_is_not_used(data_root: Path) -> None:
    lesson = embed_lesson_id(TOOL_UUID, None)
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": TOOL_UUID,
            "media_id": TOOL_UUID,
            "player_url": f"https://echo360.net.au/media/{TOOL_UUID}/public",
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row.get("recording_url", "") == ""
    assert "recording_url_source" not in row.get("metadata", {})


def test_resolved_player_id_is_not_a_standalone_link(data_root: Path) -> None:
    """``/ui/player/<id>`` returns 403 outside the Echo360 session. Do not store it."""
    lesson = embed_lesson_id(TOOL_UUID, LINK_ID)
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": TOOL_UUID,
            "link_id": LINK_ID,
            "player_url": f"http://echo360.net.au/media/{TOOL_UUID}/public",
            "resolved_media_id": MEDIA_ID,
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row.get("recording_url", "") == ""
    assert "ui/player" not in json.dumps(row)
    assert "external_tools/701" not in json.dumps(row)


def test_shared_tool_uuid_file_does_not_take_another_links_player(data_root: Path) -> None:
    generic = embed_lesson_id(TOOL_UUID, None)
    _write_vtt(data_root, generic)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": TOOL_UUID,
            "link_id": LINK_ID,
            "player_url": f"https://echo360.net.au/lti/{TOOL_UUID}?linkId={LINK_ID}",
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()
    link = resolve_recording_link(generic, data_root=data_root)
    assert link is None


def test_egress_keeps_echo360_watch_urls_and_drops_others() -> None:
    good = classroom_url(lesson_id_from_filename(COMP_STEM))
    payload = {
        "recording_url": good,
        "direct_link": "https://echo360.net.au/media/download/abc/transcript.vtt?lessonId=abc",
        "transcript": "The academic integrity policy is not the recording.",
        "cues": [
            {"timestamp": "00:01:02", "recording_url": good, "text": "dijkstra"},
            {"timestamp": "00:09:00", "recording_url": "https://evil.example/watch", "text": "no"},
        ],
    }
    cleaned = scrub_lecture_payload(payload)
    assert cleaned["recording_url"] == good
    assert cleaned["cues"][0]["recording_url"] == good
    assert cleaned["cues"][0]["timestamp"] == "00:01:02"
    assert cleaned["cues"][1]["recording_url"] == ""
    assert cleaned["direct_link"].startswith("https://echo360.net.au/media/download/")
    assert "academic integrity" in cleaned["transcript"]
    assert "t" not in parse_qs(urlparse(cleaned["recording_url"]).query)


def test_lecture_tools_return_the_recording_link(data_root: Path) -> None:
    _write_vtt(data_root, COMP_STEM)
    _write_syllabus(data_root, lesson_id_from_filename(COMP_STEM), WINDOW_START, WINDOW_END)
    from unisync.mcp.server import unisync_get_lecture_transcript, unisync_search_lectures

    found = json.loads(unisync_search_lectures(query="dijkstra", limit=3))
    assert found["total_lectures_matched"] == 1
    hit = found["results"][0]
    lesson_id = lesson_id_from_filename(COMP_STEM)
    assert hit["recording_url"] == classroom_url(lesson_id)
    assert hit["recording_url_source"] == "syllabus"
    assert hit["recording_url_kind"] == "echo360_lesson"
    assert hit["lesson_id"] == lesson_id
    assert hit["cues"][0]["timestamp"] == "00:01:02"
    assert hit["cues"][0]["recording_url"] == hit["recording_url"]
    assert "start" not in parse_qs(urlparse(hit["recording_url"]).query)

    transcript = json.loads(unisync_get_lecture_transcript(COMP_STEM, max_chars=2000))
    assert transcript["recording_url"] == hit["recording_url"]
    assert transcript["recording_url_source"] == "syllabus"
    assert transcript["recording_url_kind"] == "echo360_lesson"
    assert "[00:01:02]" in transcript["transcript"]
    assert "dijkstra" in transcript["transcript"]


def test_embed_catalog_public_media_page_is_used(data_root: Path) -> None:
    """Live check 2026-11-11: a catalog-stored /media/<id>/public page plays on its own."""
    media = "107b41dd-1d98-4fb8-8429-bf9801fcf040"
    lesson = f"BLAW_EMBED_{media}"
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": media,
            "media_id": media,
            "player_url": f"https://echo360.net.au/media/{media}",
            "canvas_launch_url": f"https://echo360.net.au/media/{media}/public",
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row["recording_url"] == f"https://echo360.net.au/media/{media}/public"
    assert row["recording_url_kind"] == "echo360_public"
    assert row["metadata"]["recording_url_source"] == "embed_catalog"


def test_access_query_params_are_stripped_and_player_urls_are_not_watchable() -> None:
    from unisync.mcp.egress import scrub_recording_url

    media = "107b41dd-1d98-4fb8-8429-bf9801fcf040"
    public = (
        f"https://echo360.net.au/media/{media}/public"
        "?autoplay=false&sessionId=abc&secureLinkAccessDataId=grant&tracking=1"
    )
    assert scrub_recording_url(public) == f"https://echo360.net.au/media/{media}/public?autoplay=false"
    player = "https://echo360.net.au/ui/player/8868926f-f6a1-42ac-a8d3-04848650d8a7?sessionId=abc&autoplay=false"
    assert scrub_recording_url(player) == ""
    lti = f"https://echo360.net.au/lti/{TOOL_UUID}?linkId={LINK_ID}&secureLinkAccessDataId=grant&sessionId=abc"
    assert scrub_recording_url(lti) == ""

    canvas = "https://canvas.lms.unimelb.edu.au/courses/240718/pages/week-3"
    payload = scrub_lecture_payload({
        "recording_url": canvas,
        "player_url": canvas,
        "classroom_url": canvas,
    })
    assert payload["recording_url"] == canvas
    assert payload["player_url"] == ""
    assert payload["classroom_url"] == ""


def test_public_page_is_preferred_over_lti_and_canvas(data_root: Path) -> None:
    media = "107b41dd-1d98-4fb8-8429-bf9801fcf040"
    lesson = embed_lesson_id(media, LINK_ID)
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": media,
            "link_id": LINK_ID,
            "player_url": f"https://echo360.net.au/lti/{media}?linkId={LINK_ID}&sessionId=sess&secureLinkAccessDataId=grant",
            "canvas_launch_url": f"https://echo360.net.au/public/media/{media}?autoplay=false&sessionId=sess",
            "canvas_page_url": "https://canvas.lms.unimelb.edu.au/courses/240718/pages/week-3",
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row["recording_url"] == f"https://echo360.net.au/public/media/{media}?autoplay=false"
    assert row["recording_url_kind"] == "echo360_public"
    assert "sessionId" not in row["recording_url"]
    assert "secureLinkAccessDataId" not in row["recording_url"]


def test_canvas_page_is_used_when_no_public_page_exists(data_root: Path) -> None:
    lesson = embed_lesson_id(MEDIA_ID, LINK_ID)
    nested = (
        f"https://echo360.net.au/lti/{MEDIA_ID}?linkId={LINK_ID}"
        "&sessionId=sess&secureLinkAccessDataId=grant"
    )
    canvas = (
        "https://canvas.lms.unimelb.edu.au/courses/240718/external_tools/retrieve"
        f"?display=borderless&url={quote(nested, safe='')}"
    )
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": MEDIA_ID,
            "link_id": LINK_ID,
            "player_url": f"https://echo360.net.au/lti/{MEDIA_ID}?linkId={LINK_ID}",
            "canvas_launch_url": canvas,
            "resolved_media_id": MEDIA_ID,
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    row = _manifest_item(data_root, lesson)
    assert row["recording_url_kind"] == "canvas_embed_page"
    assert row["recording_url"].startswith(
        "https://canvas.lms.unimelb.edu.au/courses/240718/external_tools/retrieve?"
    )
    assert "secureLinkAccessDataId" not in row["recording_url"]
    assert "sessionId" not in unquote(row["recording_url"])
    assert "linkId" in unquote(row["recording_url"])
    assert "ui/player" not in row["recording_url"]


def test_lecture_capture_url_is_used_only_when_the_catalog_records_it(data_root: Path) -> None:
    lesson = embed_lesson_id(MEDIA_ID, LINK_ID)
    capture = "https://canvas.lms.unimelb.edu.au/courses/240718/external_tools/701"
    _write_vtt(data_root, lesson)
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": MEDIA_ID,
            "link_id": LINK_ID,
            "player_url": f"https://echo360.net.au/ui/player/{MEDIA_ID}?sessionId=sess",
        }]
    }), encoding="utf-8")
    echo360_links._json_cache.clear()

    missing = _manifest_item(data_root, lesson)
    assert missing["recording_url"] == ""
    assert missing["recording_url_kind"] == "unplayable_lti"
    assert "701" not in missing["recording_url"]

    (data_root / "libraries" / "libraries.json").write_text(json.dumps({
        "blaw": {
            "canvas_course_id": "240718",
            "lecture_capture_url": capture,
        }
    }), encoding="utf-8")
    echo360_links._json_cache.clear()
    (data_root / "downloads_manifest.json").unlink()
    recorded = _manifest_item(data_root, lesson)
    assert recorded["recording_url"] == capture
    assert recorded["recording_url_kind"] == "canvas_embed_page"
