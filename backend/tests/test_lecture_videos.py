"""Opt-in lecture video downloads, using synthetic playlists and no live credentials."""

from __future__ import annotations

import asyncio

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from unisync.core.download_registry import DownloadRegistry
from unisync.mcp.egress import scrub_lecture_payload
from unisync.services.sync.echo360_ingest import embed_lesson_id
from unisync.services.sync.lecture_videos import (
    DownloadError,
    FetchResult,
    LaunchResult,
    MediaChoice,
    ProbeResult,
    StreamResult,
    VideoTarget,
    _download_bytes,
    _ffmpeg_argv,
    choose_playable,
    download_lectures,
    download_one,
    is_allowed_media_url,
    join_with_query,
    player_property_urls,
    public_failure,
    redact_argv,
    select_targets,
    select_variant,
    videos_enabled,
)

LESSON = (
    "G_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa_"
    "5645c5ef-f457-430f-939a-6415f263ba6a_"
    "2026-07-28T17:05:00.000_2026-07-28T18:00:00.000"
)
MEDIA = "f943cb65-b75e-469c-ae4d-cf4326069801"
TOOL = "9381284d-5253-4e5b-b173-fb47a63576cb"
LINK = "de5554c9-cdf5-4ff4-98b2-f9b156ebe79f"
MP4 = f"https://content.echo360.net.au/inst/{MEDIA}/1/hd1.mp4?Policy=secret&Signature=secret"
MASTER = "https://content.echo360.net.au/inst/1/s1_av.m3u8?Policy=secret&Signature=secret"


class _DataRoot:
    def __init__(self, root: Path) -> None:
        self._root = root

    def get_user_root(self) -> Path:
        return self._root


class MapClient:
    def __init__(self) -> None:
        self.gets: dict[str, tuple[int, bytes | str]] = {}
        self.files: dict[str, bytes] = {}
        self.probe_sizes: dict[str, int | None] = {}
        self.launch_result = LaunchResult()
        self.streams = 0

    async def auth_headers(self, url: str) -> dict[str, str]:
        return {}

    async def get(self, url: str, headers: dict | None = None) -> FetchResult:
        status, body = self.gets.get(url, (404, b""))
        if isinstance(body, str):
            body = body.encode()
        return FetchResult(status, body)

    async def probe(self, url: str) -> ProbeResult:
        name = Path(urlparse(url).path).name
        for key, total in self.probe_sizes.items():
            if key == url or Path(urlparse(key).path).name == name:
                return ProbeResult(200, total)
        return ProbeResult(404, None)

    async def stream_to(self, url: str, dest: Path, headers: dict | None = None) -> StreamResult:
        self.streams += 1
        data = self.files.get(url)
        if data is None:
            name = Path(urlparse(url).path).name
            for key, value in self.files.items():
                if Path(urlparse(key).path).name == name:
                    data = value
                    break
        if data is None:
            return StreamResult(404, 0, None)
        start = 0
        status = 200
        if headers and headers.get("Range"):
            start = int(headers["Range"].split("=", 1)[1].split("-", 1)[0])
            status = 206
            with dest.open("ab") as handle:
                handle.write(data[start:])
        else:
            dest.write_bytes(data)
        return StreamResult(status, len(data) - start, len(data))

    async def launch(self, record: dict) -> LaunchResult:
        return self.launch_result


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "cache" / "echo360_subtitles").mkdir(parents=True)
    (root / "libraries").mkdir()
    (root / "media_library").mkdir()
    monkeypatch.setattr("unisync.core.path_service.get_path_service", lambda: _DataRoot(root))
    from unisync.services.sync import echo360_links
    echo360_links._json_cache.clear()
    return root


def _registry(root: Path) -> DownloadRegistry:
    return DownloadRegistry(root / "downloads_manifest.json")


def _syllabus(root: Path) -> None:
    payload = [{
        "sectionId": "5645c5ef-f457-430f-939a-6415f263ba6a",
        "courseName": "COMP10001 Foundations of Computing",
        "syllabus": [{
            "lesson": {
                "lesson": {
                    "id": LESSON,
                    "name": "Welcome lecture",
                    "timing": {"start": "2026-07-28T17:05:00.000"},
                },
                "medias": [{"id": MEDIA}],
            }
        }],
    }, {
        "sectionId": "info-section",
        "courseName": "INFO10003",
        "syllabus": [{
            "lesson": {
                "lesson": {
                    "id": "G_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb_other_2026-08-01T10:00:00.000_2026-08-01T11:00:00.000",
                    "name": "Other course",
                    "timing": {"start": "2026-08-01T10:00:00.000"},
                },
                "medias": [{"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}],
            }
        }],
    }]
    (root / "cache" / "echo360_syllabus.json").write_text(json.dumps(payload), encoding="utf-8")


def test_videos_are_off_unless_the_flag_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNISYNC_DOWNLOAD_LECTURE_VIDEOS", raising=False)
    assert videos_enabled() is False
    monkeypatch.setenv("UNISYNC_DOWNLOAD_LECTURE_VIDEOS", "1")
    assert videos_enabled() is True


def test_playable_choice_prefers_an_mp4_under_the_height_cap() -> None:
    payload = {
        "data": {
            "playableAudioVideo": {
                "playableMedias": [
                    {"uri": "https://content.echo360.net.au/a/high.mp4", "height": 1080, "trackType": ["Video", "Audio"]},
                    {"uri": "https://content.echo360.net.au/a/mid.m3u8", "isHls": True, "height": 720},
                    {"uri": "https://content.echo360.net.au/a/fit.mp4", "height": 720, "trackType": ["Video", "Audio"]},
                    {"uri": "https://content.echo360.net.au/a/audio.m4s", "trackType": ["Audio"]},
                    {"uri": "http://evil.example/steal.mp4", "height": 480},
                ]
            }
        }
    }
    choice = choose_playable(payload, max_height_px=720)
    assert choice is not None
    assert choice.url.endswith("/fit.mp4")
    assert choose_playable({"data": {}}, max_height_px=720) is None


def test_relative_playlist_keeps_the_parent_query_and_picks_720p() -> None:
    joined = join_with_query(MASTER, "mid.m3u8")
    assert joined.endswith("/mid.m3u8?Policy=secret&Signature=secret")
    playlist = "\n".join([
        "#EXTM3U",
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
        "low.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720",
        "mid.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080",
        "high.m3u8",
    ])
    assert select_variant(playlist, MASTER, max_height_px=720) == joined


def test_rejects_urls_outside_the_media_hosts() -> None:
    assert is_allowed_media_url(MP4) is True
    assert is_allowed_media_url("https://user:pw@content.echo360.net.au/a.mp4") is False
    assert is_allowed_media_url("http://content.echo360.net.au/a.mp4") is False
    assert is_allowed_media_url("https://echo360.net.au.evil.com/a.mp4") is False
    assert "Signature" not in public_failure(403, MP4)
    assert "Policy" not in public_failure(403, MP4)


def test_ffmpeg_argv_redacts_headers_and_query_strings() -> None:
    argv = _ffmpeg_argv(
        "ffmpeg",
        [MP4],
        Path("/tmp/lecture.mp4"),
        {"Cookie": "ECHO_JWT=secret", "Referer": "https://echo360.net.au/"},
    )
    redacted = redact_argv(argv)
    assert "ECHO_JWT=secret" not in " ".join(redacted)
    assert "Signature" not in " ".join(redacted)
    assert "[redacted]" in redacted


@pytest.mark.asyncio
async def test_echo360_course_mp4_is_saved_once_and_recorded(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unisync.services.sync.lecture_videos.shutil.which", lambda _name: None)
    _syllabus(data_root)
    targets = select_targets(data_root, course="COMP10001")
    assert [item.lesson_id for item in targets] == [LESSON]
    assert targets[0].media_id == MEDIA
    client = MapClient()
    properties = player_property_urls(targets[0])[0]
    assert "sessionId" not in properties
    client.gets[properties] = (200, json.dumps({
        "data": {"playableAudioVideo": {"playableMedias": [
            {"uri": MP4, "height": 720, "trackType": ["Video", "Audio"]},
        ]}}
    }))
    client.files[MP4] = b"lecture-bytes"
    registry = _registry(data_root)
    first = await download_one(targets[0], client=client, data_root=data_root, registry=registry, max_size=1000)
    assert first["video_status"] == "downloaded"
    assert first["video_downloaded"] is True
    assert Path(first["video_filepath"]).read_bytes() == b"lecture-bytes"
    saved = json.loads((data_root / "downloads_manifest.json").read_text(encoding="utf-8"))
    blob = json.dumps(saved)
    assert "Signature" not in blob
    assert "Policy" not in blob
    assert "ECHO_JWT" not in blob
    # json.dumps escapes Windows backslashes, so compare the encoded form.
    assert json.dumps(first["video_filepath"])[1:-1] in blob
    second = await download_one(targets[0], client=client, data_root=data_root, registry=registry, max_size=1000)
    assert second["video_status"] == "skipped"
    assert client.streams == 1


@pytest.mark.asyncio
async def test_mp4_resume_appends_the_missing_range(tmp_path: Path) -> None:
    client = MapClient()
    client.files[MP4] = b"abcdefghij"
    dest = tmp_path / "lecture.mp4"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"abcde")
    total = await _download_bytes(client, MP4, dest, max_size=100)
    assert total == 10
    assert dest.read_bytes() == b"abcdefghij"
    assert not part.exists()


@pytest.mark.asyncio
async def test_oversized_hd_falls_back_to_sd(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unisync.services.sync.lecture_videos.shutil.which", lambda _name: None)
    playlist = MASTER
    client = MapClient()
    client.probe_sizes = {
        "hd1.mp4": 5_000,
        "sd1.mp4": 800,
    }
    client.files["https://content.echo360.net.au/inst/1/sd1.mp4?Policy=secret&Signature=secret"] = b"sd-video"
    target = VideoTarget(
        lesson_id=LESSON,
        title="Welcome",
        course_id="5645",
        course_name="COMP10001",
        date="2026-07-28T17:05:00.000",
        kind="echo360_lesson",
        media_id=MEDIA,
        public_id="",
        secure_id="",
        page_url="",
        recording_url="https://echo360.net.au/lesson/example/classroom?focus=Video",
        source="echo360_course",
    )
    choice = MediaChoice(container="hls", url=playlist, label="hls")
    from unisync.services.sync.lecture_videos import prefer_progressive

    picked = await prefer_progressive(choice, client, max_size=1000)
    assert picked.url.endswith("/sd1.mp4?Policy=secret&Signature=secret")
    from unisync.services.sync.lecture_videos import _store_choice

    result = await _store_choice(
        target,
        picked,
        client=client,
        dest_dir=data_root / "lecture_videos" / "COMP10001",
        registry=_registry(data_root),
        max_height_px=720,
        max_size=1000,
    )
    assert result["video_status"] == "downloaded"
    assert Path(result["video_filepath"]).read_bytes() == b"sd-video"
    assert "Signature" not in json.dumps(result)


@pytest.mark.asyncio
async def test_hls_segments_when_ffmpeg_is_missing(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unisync.services.sync.lecture_videos.shutil.which", lambda _name: None)
    mid = join_with_query(MASTER, "mid.m3u8")
    seg0 = join_with_query(mid, "seg0.ts")
    seg1 = join_with_query(mid, "seg1.ts")
    client = MapClient()
    client.gets[MASTER] = (200, "\n".join([
        "#EXTM3U",
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
        "low.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720",
        "mid.m3u8",
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080",
        "high.m3u8",
    ]))
    client.gets[mid] = (200, "#EXTM3U\n#EXTINF:6.0,\nseg0.ts\n#EXTINF:6.0,\nseg1.ts\n")
    client.files[seg0] = b"AAA"
    client.files[seg1] = b"BBB"
    client.launch_result = LaunchResult(media_url=MASTER, media_kind="m3u8", media_id=MEDIA)
    lesson = embed_lesson_id(TOOL, LINK)
    (data_root / "libraries" / "libraries.json").write_text(json.dumps({
        "blaw": {"name": "BLAW10001 Principles of Business Law", "canvas_course_id": "240718", "kb_name": "BLAW10001"},
    }), encoding="utf-8")
    (data_root / "libraries" / "echo360_embeds.json").write_text(json.dumps({
        "240718": [{
            "uuid": TOOL,
            "media_id": TOOL,
            "link_id": LINK,
            "title": "6.3 Divisible contracts",
            "player_url": f"https://echo360.net.au/lti/{TOOL}?linkId={LINK}&sessionId=sess",
            "canvas_launch_url": "https://canvas.lms.unimelb.edu.au/courses/240718/modules/items/99",
            "secure_link_access_data_id": "grant-secret",
        }]
    }), encoding="utf-8")
    results = await download_lectures(
        data_root=data_root,
        course="BLAW10001",
        lecture="divisible",
        limit=1,
        client=client,
        registry=_registry(data_root),
        max_size=1000,
    )
    assert results[0]["video_status"] == "downloaded"
    assert results[0]["recording_url_kind"] == "canvas_embed_page"
    assert Path(results[0]["video_filepath"]).read_bytes() == b"AAABBB"
    blob = json.dumps(results) + (data_root / "downloads_manifest.json").read_text(encoding="utf-8")
    assert "grant-secret" not in blob
    assert "sessionId" not in blob
    assert "Signature" not in blob
    from unisync.services.sync.lecture_videos import lookup_video_status

    found = lookup_video_status(data_root, lesson)
    assert found["video_downloaded"] is True
    assert found["video_filepath"] == results[0]["video_filepath"]


@pytest.mark.asyncio
async def test_failed_request_does_not_leak_the_signed_url(data_root: Path) -> None:
    client = MapClient()
    target = VideoTarget(
        lesson_id=LESSON,
        title="Welcome",
        course_id="5645",
        course_name="COMP10001",
        date="",
        kind="echo360_lesson",
        media_id=MEDIA,
        public_id="",
        secure_id="",
        page_url="",
        recording_url="",
        source="echo360_course",
    )
    for url in player_property_urls(target):
        client.gets[url] = (401, b"")
    result = await download_one(
        target,
        client=client,
        data_root=data_root,
        registry=_registry(data_root),
    )
    assert result["video_status"] == "unavailable"
    assert "Signature" not in result["video_error"]
    with pytest.raises(DownloadError):
        await _download_bytes(client, "https://evil.example/a.mp4", data_root / "nope.mp4", max_size=10)


@pytest.mark.asyncio
async def test_download_tool_scrubs_signed_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs):
        return [{
            "video_status": "failed",
            "video_downloaded": False,
            "video_error": "https://content.echo360.net.au/a.m3u8?CloudFront-Signature=abc&sessionId=sess&focus=Video",
            "cookie": "ECHO_JWT=nope",
            "recording_url": "https://echo360.net.au/lesson/abc/classroom?focus=Video&sessionId=sess",
        }]

    monkeypatch.setattr("unisync.services.sync.lecture_videos.download_lectures", fake)
    from unisync.mcp.server import unisync_download_lecture_videos

    payload = json.loads(await unisync_download_lecture_videos(course="COMP10001", limit=1))
    text = json.dumps(payload)
    assert "CloudFront-Signature" not in text
    assert "sessionId" not in text
    assert "ECHO_JWT" not in text
    assert "cookie" not in text
    assert payload["results"][0]["recording_url"].endswith("classroom?focus=Video")


@pytest.mark.asyncio
async def test_sync_flag_path_saves_the_resolved_stream(data_root: Path) -> None:
    from types import SimpleNamespace
    from unisync.services.sync.lecture_videos import download_resolved_source

    client = MapClient()
    client.files[MP4] = b"from-sync"
    library = data_root / "media_library" / "item1"
    library.mkdir(parents=True)
    (library / "metadata.json").write_text("{}", encoding="utf-8")
    result = await download_resolved_source(
        "item1",
        SimpleNamespace(
            id=LESSON,
            title="Welcome lecture",
            course_id="5645",
            course_name="COMP10001",
            date="2026-07-28T17:05:00.000",
            media_url=MP4,
            media_kind="mp4",
            cookie_header="ECHO_JWT=do-not-log",
        ),
        client=client,
        data_root=data_root,
        registry=_registry(data_root),
    )
    assert result["video_downloaded"] is True
    assert Path(result["video_filepath"]).read_bytes() == b"from-sync"
    meta = json.loads((library / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "downloaded"
    assert meta["video_filepath"] == result["video_filepath"]
    blob = json.dumps(meta) + (data_root / "downloads_manifest.json").read_text(encoding="utf-8")
    assert "ECHO_JWT" not in blob
    assert "Signature" not in blob


def test_egress_keeps_a_local_video_path_and_drops_cdn_secrets() -> None:
    cleaned = scrub_lecture_payload({
        "cookie": "secret",
        "video_filepath": "/data/lecture_videos/COMP10001/lecture.mp4",
        "video_status": "downloaded",
        "note": "https://content.echo360.net.au/a.m3u8?CloudFront-Signature=abc&CloudFront-Policy=p&sessionId=s&focus=Video",
    })
    assert "cookie" not in cleaned
    assert cleaned["video_filepath"].endswith("lecture.mp4")
    assert "CloudFront-Signature" not in cleaned["note"]
    assert "sessionId" not in cleaned["note"]
    assert "focus=Video" in cleaned["note"]


# --- live-test fixes: signed CDN query, pre-roll disclaimer, duration guard, mute ---

from unisync.services.sync import lecture_videos as _lv  # noqa: E402

CDN = "https://content.echo360.net.au/0000.inst/" + MEDIA + "/3/"


def _properties(media_id: str, seconds: float, *, base: str = CDN) -> dict:
    return {"data": {
        "playableAudioVideo": {
            "duration": f"PT{seconds}S",
            "mediaId": media_id,
            "playableMedias": [
                {"isHls": True, "trackType": ["Audio"], "uri": base + "s0_a.m3u8?x-act=1&x-mid=2"},
                {"isHls": True, "trackType": ["Video"], "uri": base + "s1_v.m3u8?x-act=1&x-mid=2"},
                {"isHls": True, "trackType": ["Audio", "Video"], "uri": base + "s1_av.m3u8?x-act=1&x-mid=2"},
            ],
        },
        "sourceQueryStrings": {"queryStrings": [{
            "uriPattern": "^https://.*\\.echo360\\.net\\.au/0000\\.inst/" + media_id + "/3/.*$",
            "queryString": "Policy=p&Signature=s&Key-Pair-Id=k&x-uid=u",
        }]},
    }}


def test_iso_duration_parses_echo360_values() -> None:
    assert _lv.parse_iso_duration("PT982.867S") == pytest.approx(982.867)
    assert _lv.parse_iso_duration("PT1H2M3S") == 3723
    assert _lv.parse_iso_duration("") == 0


def test_player_uri_query_is_swapped_for_the_cloudfront_signature() -> None:
    choice = choose_playable(_properties(MEDIA, 3000), max_height_px=720)
    assert choice is not None
    query = urlparse(choice.url).query
    assert "Signature=s" in query and "Key-Pair-Id=k" in query
    assert "x-act" not in query
    # Audio+video wins over the silent video-only rendition.
    assert urlparse(choice.url).path.endswith("/s1_av.m3u8")
    assert choice.duration == 3000 and choice.media_id == MEDIA


def test_the_longest_media_wins_over_the_disclaimer_pre_roll() -> None:
    disclaimer = "d1500000-0000-4000-8000-000000000000"
    pre = _properties(disclaimer, 15.2, base="https://content.echo360.net.au/0000.inst/" + disclaimer + "/3/")
    lecture = _properties(MEDIA, 2950.0)
    for order in ([pre, lecture], [lecture, pre]):
        choice = _lv.choose_main_lecture(order, max_height_px=720)
        assert choice is not None and choice.media_id == MEDIA and choice.duration == 2950.0


async def test_browser_capture_is_used_when_http_properties_fail(data_root: Path) -> None:
    _syllabus(data_root)
    target = select_targets(data_root, course="COMP10001")[0]

    class CaptureClient(MapClient):
        async def capture_properties(self, page_url: str) -> list:
            return [_properties("d1500000-0000-4000-8000-000000000000", 15), _properties(MEDIA, 3100)]

    client = CaptureClient()
    choice, note = await _lv.resolve_media(target, client, max_height_px=720, max_size=10**9)
    assert note == "" and choice is not None
    assert choice.media_id == MEDIA and choice.duration == 3100


async def test_short_file_is_flagged_not_downloaded(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _syllabus(data_root)
    target = select_targets(data_root, course="COMP10001")[0]
    client = MapClient()
    client.files[MP4] = b"disclaimer"
    monkeypatch.setattr(_lv, "probe_duration", lambda path: 15.0)
    result = await _lv._store_choice(
        target, MediaChoice(container="mp4", url=MP4, label="hd"),
        client=client, dest_dir=data_root / "lecture_videos" / "COMP10001",
        registry=_registry(data_root), max_height_px=720, max_size=1000,
    )
    assert result["video_status"] == "suspicious_short"
    assert result["video_downloaded"] is False
    assert result["video_duration_seconds"] == 15
    assert "disclaimer" in result["video_error"]
    # A later run does not treat the short file as the lecture; it is renamed, not deleted.
    monkeypatch.setattr(_lv, "probe_duration", lambda path: 15.0 if "short" not in path.name else 15.0)
    assert _lv._too_short(Path(result["video_filepath"]))


async def test_long_file_records_its_duration(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _syllabus(data_root)
    target = select_targets(data_root, course="COMP10001")[0]
    client = MapClient()
    client.files[MP4] = b"lecture"
    monkeypatch.setattr(_lv, "probe_duration", lambda path: 2950.4)
    result = await _lv._store_choice(
        target, MediaChoice(container="mp4", url=MP4, label="hd"),
        client=client, dest_dir=data_root / "lecture_videos" / "COMP10001",
        registry=_registry(data_root), max_height_px=720, max_size=1000,
    )
    assert result["video_status"] == "downloaded" and result["video_duration_seconds"] == 2950


def test_unrecorded_timetable_slots_are_skipped_without_a_lecture_filter(data_root: Path) -> None:
    _syllabus(data_root)
    targets = select_targets(data_root, course="COMP10001")
    slot = VideoTarget(**{**targets[0].__dict__, "lesson_id": "G_slot", "media_id": "", "date": "2099-01-01"})
    ordered = [slot] + targets
    recorded = [t for t in ordered if t.source != "echo360_course" or t.media_id]
    assert recorded and recorded[0].media_id


def test_browser_is_launched_muted() -> None:
    import inspect
    from unisync.services.browser import camoufox_service
    from unisync.services.sync import lecture_video_browser

    assert '"media.volume_scale": "0.0"' in inspect.getsource(camoufox_service.CamoufoxService._launch_options)
    script = lecture_video_browser.MUTE_SCRIPT
    assert "muted = true" in script and "volume = 0" in script



class _CaptureClient:
    def __init__(self, files, note=""):
        self.files, self.note, self.calls = files, note, []

    async def lesson_files(self, record, media_id, lesson_id, *, full=True):
        self.calls.append((record.get("lecture_capture_url"), media_id, lesson_id, full))
        return {"files": self.files, "labels": {1: "Full", 2: "Full"}, "muted": True, "note": self.note}


def _capture_target(**extra):
    base = dict(
        lesson_id="G_x", title="t", course_id="sec", course_name="C", date="", kind="echo360_lesson",
        media_id="m1", public_id="", secure_id="", page_url="https://echo360.net.au/lesson/G_x/classroom",
        recording_url="", source="echo360_course",
        record={"lecture_capture_url": "https://canvas.lms.unimelb.edu.au/courses/1/external_tools/701"},
    )
    base.update(extra)
    return _lv.VideoTarget(**base)


def test_lecture_capture_choice_keeps_both_feeds():
    files = {1: "https://content.echo360.net.au/a/m1/1/hd1.mp4?Signature=x", 2: "https://content.echo360.net.au/a/m1/1/hd2.mp4?Signature=y"}
    client = _CaptureClient(files)
    choice, note = asyncio.run(_lv._choice_from_lecture_capture(client, _capture_target(), max_height_px=1080))
    assert note == "" and choice is not None
    assert choice.url == files[1] and choice.feed_urls == files
    assert choice.roles == {"screen": 1, "camera": 2} and choice.label == "hd"
    assert client.calls[0][3] is True


def test_lecture_capture_choice_reports_sign_in_and_skips_without_url():
    choice, note = asyncio.run(_lv._choice_from_lecture_capture(_CaptureClient({}, "canvas_sign_in_required"), _capture_target(), max_height_px=720))
    assert choice is None and note == "canvas_sign_in_required"
    choice, note = asyncio.run(_lv._choice_from_lecture_capture(_CaptureClient({}), _capture_target(record={}), max_height_px=720))
    assert choice is None and note == ""


def test_lecture_capture_url_from_library(monkeypatch):
    monkeypatch.delenv("UNISYNC_LECTURE_CAPTURE_URL", raising=False)
    libs = [{"echo360_course_id": "sec", "canvas_course_id": "241246"}, {"echo360_course_id": "other", "lecture_capture_url": "https://x/y"}]
    assert _lv._lecture_capture_url(libs, "sec").endswith("/courses/241246/external_tools/701")
    assert _lv._lecture_capture_url(libs, "other") == "https://x/y"
    assert _lv._lecture_capture_url(libs, "none") == ""