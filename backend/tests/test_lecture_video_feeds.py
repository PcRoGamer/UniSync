"""Two-feed lectures and the picture-in-picture file, with synthetic clips only."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from unisync.core.download_registry import DownloadRegistry
from unisync.services.sync import lecture_video_feeds as feeds
from unisync.services.sync import lecture_videos as lv

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
CDN = "https://content.echo360.net.au/0000.inst/f943cb65-b75e-469c-ae4d-cf4326069801/3/"
SIGNED = "?Policy=p&Signature=s&Key-Pair-Id=k"


def _two_feed_root() -> dict:
    return {"playableAudioVideo": {"duration": "PT3000S", "playableMedias": [
        {"isHls": True, "trackType": ["Audio"], "sourceIndex": 0, "uri": CDN + "s0_a.m3u8"},
        {"isHls": True, "trackType": ["Video"], "sourceIndex": 1, "uri": CDN + "s1_v.m3u8"},
        {"isHls": True, "trackType": ["Audio", "Video"], "sourceIndex": 1, "uri": CDN + "s1_av.m3u8"},
        {"isHls": True, "trackType": ["Video"], "sourceIndex": 2, "uri": CDN + "s2_v.m3u8"},
        {"isHls": True, "trackType": ["Audio", "Video"], "sourceIndex": 2, "uri": CDN + "s2_av.m3u8"},
    ]}}


def test_sources_and_roles_from_player_properties() -> None:
    root = _two_feed_root()
    assert feeds.video_sources(root) == [1, 2]
    assert feeds.assign_roles(root, [1, 2]) == {"screen": 1, "camera": 2}
    named = dict(root, sources=[{"sourceIndex": 1, "name": "Camera"}, {"sourceIndex": 2, "name": "Computer display"}])
    assert feeds.assign_roles(named, [1, 2]) == {"camera": 1, "screen": 2}
    one = {"playableAudioVideo": {"playableMedias": [root["playableAudioVideo"]["playableMedias"][2]]}}
    assert feeds.assign_roles(one, feeds.video_sources(one)) == {"screen": 1}


def test_choose_playable_carries_roles() -> None:
    choice = lv.choose_playable(_two_feed_root(), max_height_px=720)
    assert choice is not None and choice.roles == {"screen": 1, "camera": 2}


def test_file_name_helpers() -> None:
    assert feeds.source_of_url(CDN + "hd2.mp4" + SIGNED) == 2
    assert feeds.source_of_url(CDN + "s1q1.m4s") == 1
    assert feeds.source_of_url(CDN + "s1_av.m3u8") == 1
    assert feeds.source_of_url("https://x/a/lecture.mp4") == 0
    assert feeds.durations_match(3000, 2990) and not feeds.durations_match(3000, 15)
    assert feeds.feed_path(Path("d"), "L", "camera").name == "L.camera.mp4"
    assert feeds.pip_path(Path("d"), "L").name == "L.pip.mp4"


def test_pip_settings_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    default = feeds.pip_settings()
    assert default.enabled and default.size == 0.22 and default.position == "bottom-right"
    graph = feeds.pip_filter(1920, default)
    assert "scale=422:422" in graph and "alphamerge" in graph and "hypot" in graph
    assert "overlay=W-w-38:H-h-38" in graph
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP", "0")
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_SIZE", "0.25")
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_POSITION", "top-left")
    custom = feeds.pip_settings()
    assert not custom.enabled and custom.size == 0.25
    assert "overlay=38:38" in feeds.pip_filter(1920, custom)
    argv = feeds.pip_argv("ffmpeg", Path("s.mp4"), Path("c.mp4"), Path("p.mp4"), screen_width=1920,
                          settings=default, encoder="libx264")
    assert argv[argv.index("-crf") + 1] == "23" and argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-threads") + 1] == "2"


def _clip(path: Path, size: str, *, audio: bool, seconds: int = 2) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=10:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_build_pip_keeps_screen_size_and_audio(tmp_path: Path) -> None:
    screen, camera = tmp_path / "L.screen.mp4", tmp_path / "L.camera.mp4"
    _clip(screen, "320x180", audio=True)
    _clip(camera, "160x90", audio=False)
    out, encoder = await feeds.build_pip(screen, camera, tmp_path / "L.pip.mp4",
                                         feeds.PipSettings(encoder="libx264"))
    assert out is not None and encoder == "libx264"
    streams = _probe(out)["streams"]
    video = [s for s in streams if s["codec_type"] == "video"][0]
    assert (video["width"], video["height"]) == (320, 180)
    assert any(s["codec_type"] == "audio" for s in streams)


async def test_pip_disabled_returns_reason(tmp_path: Path) -> None:
    out, why = await feeds.build_pip(tmp_path / "a", tmp_path / "b", tmp_path / "c", feeds.PipSettings(enabled=False))
    assert out is None and why == "pip disabled"


class _FeedClient:
    """Serves hd1.mp4 / hd2.mp4 beside the lecture playlist."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def auth_headers(self, url: str) -> dict:
        return {}

    async def probe(self, url: str) -> lv.ProbeResult:
        name = Path(urlparse(url).path).name
        return lv.ProbeResult(200, len(self.files[name])) if name in self.files else lv.ProbeResult(404, None)

    async def stream_to(self, url: str, dest: Path, headers: dict | None = None) -> lv.StreamResult:
        data = self.files.get(Path(urlparse(url).path).name)
        if data is None:
            return lv.StreamResult(404, 0, None)
        dest.write_bytes(data)
        return lv.StreamResult(200, len(data), len(data))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_two_feed_lecture_saves_screen_camera_and_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screen_src, camera_src = tmp_path / "s.mp4", tmp_path / "c.mp4"
    _clip(screen_src, "320x180", audio=True)
    _clip(camera_src, "160x90", audio=True)
    client = _FeedClient({"hd1.mp4": screen_src.read_bytes(), "hd2.mp4": camera_src.read_bytes()})
    monkeypatch.setattr(lv, "probe_duration", lambda path: 3000.0)
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_ENCODER", "libx264")
    data_root = tmp_path / "data"
    registry = DownloadRegistry(data_root / "downloads_manifest.json")
    target = lv.VideoTarget(
        lesson_id="G_lecture", title="W1 L1", course_id="sec", course_name="COMP10001_2026_SM2", date="2026-07-28",
        kind="echo360_lesson", media_id="m", public_id="", secure_id="", page_url="", recording_url="", source="echo360_course",
    )
    choice = lv.MediaChoice(container="mp4", url=CDN + "hd1.mp4" + SIGNED, label="hd", duration=3000.0,
                            roles={"screen": 1, "camera": 2})
    dest = data_root / "lecture_videos" / "COMP10001"
    result = await lv._store_choice(target, choice, client=client, dest_dir=dest, registry=registry,
                                    max_height_px=720, max_size=10**9)
    assert result["video_status"] == "downloaded"
    assert Path(result["video_screen_filepath"]).name == "G_lecture.screen.mp4"
    assert Path(result["video_camera_filepath"]).name == "G_lecture.camera.mp4"
    assert Path(result["video_pip_filepath"]).name == "G_lecture.pip.mp4"
    assert result["video_filepath"] == result["video_pip_filepath"] and result["video_feeds"] == "2"
    for key in ("video_screen_filepath", "video_camera_filepath", "video_pip_filepath"):
        assert Path(result[key]).is_file()
    manifest = json.loads((data_root / "downloads_manifest.json").read_text(encoding="utf-8"))
    rows = [row for row in manifest["items"].values() if str(row.get("item_id", "")).endswith(":video")]
    assert rows and rows[0]["metadata"]["video_pip_filepath"] == result["video_pip_filepath"]
    fields = lv._fields_from_row(rows[0])
    assert fields["video_camera_filepath"] == result["video_camera_filepath"] and fields["video_feeds"] == 2
    blob = json.dumps(manifest)
    assert "Signature" not in blob and "Policy" not in blob
    # A second run finds the pip file and skips.
    again = await lv._store_choice(target, choice, client=client, dest_dir=dest, registry=registry,
                                   max_height_px=720, max_size=10**9)
    assert again["video_status"] == "skipped"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_second_feed_of_the_wrong_length_is_set_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screen_src, camera_src = tmp_path / "s.mp4", tmp_path / "c.mp4"
    _clip(screen_src, "320x180", audio=True)
    _clip(camera_src, "160x90", audio=True)
    client = _FeedClient({"hd1.mp4": screen_src.read_bytes(), "hd2.mp4": camera_src.read_bytes()})
    monkeypatch.setattr(lv, "probe_duration", lambda path: 15.0 if "camera" in path.name else 3000.0)
    data_root = tmp_path / "data"
    target = lv.VideoTarget(
        lesson_id="G_x", title="t", course_id="sec", course_name="COMP10001", date="", kind="echo360_lesson",
        media_id="m", public_id="", secure_id="", page_url="", recording_url="", source="echo360_course",
    )
    choice = lv.MediaChoice(container="mp4", url=CDN + "hd1.mp4" + SIGNED, label="hd", duration=3000.0,
                            roles={"screen": 1, "camera": 2})
    result = await lv._store_choice(target, choice, client=client, dest_dir=data_root / "v",
                                    registry=DownloadRegistry(data_root / "downloads_manifest.json"),
                                    max_height_px=720, max_size=10**9)
    assert result["video_status"] == "downloaded"
    assert "video_camera_filepath" not in result and "video_pip_filepath" not in result
    assert Path(result["video_filepath"]).name == "G_x.screen.mp4"
