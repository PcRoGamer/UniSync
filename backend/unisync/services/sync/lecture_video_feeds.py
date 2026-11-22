"""Two-feed Echo360 lectures: screen capture + lecturer camera, and a picture-in-picture file.

Echo360 numbers its sources: ``s0`` is the audio, ``s1``/``s2`` are video feeds.
A two-feed lecture has both ``s1`` and ``s2``. Each feed is saved on its own
(``<lecture>.screen.mp4`` / ``<lecture>.camera.mp4``) with the lecture audio,
then ffmpeg builds ``<lecture>.pip.mp4``: screen full frame, camera as a small
circle in a corner.

Settings (environment):

* ``UNISYNC_LECTURE_VIDEO_PIP`` - ``0`` turns the combined file off (default on).
* ``UNISYNC_LECTURE_VIDEO_PIP_SIZE`` - circle diameter as a share of the screen width (default 0.22).
* ``UNISYNC_LECTURE_VIDEO_PIP_POSITION`` - ``bottom-right`` (default), ``bottom-left``, ``top-right``, ``top-left``.
* ``UNISYNC_LECTURE_VIDEO_PIP_MARGIN`` - gap to the frame edge as a share of the width (default 0.02).
* ``UNISYNC_LECTURE_VIDEO_PIP_ENCODER`` - ``auto`` (default: a working hardware H.264 encoder, else libx264),
  or an ffmpeg encoder name.
* ``UNISYNC_LECTURE_VIDEO_PIP_CRF`` / ``UNISYNC_LECTURE_VIDEO_PIP_PRESET`` - libx264 quality (23 / veryfast).
* ``UNISYNC_LECTURE_VIDEO_FFMPEG_THREADS`` - encoder threads (default 2, so the PC stays usable).
* ``UNISYNC_LECTURE_VIDEO_PIP_CROP`` / ``UNISYNC_LECTURE_VIDEO_PIP_TRACK`` and the other tracking
  settings are described in ``lecture_video_tracking``: the camera circle follows the presenter
  when OpenCV is installed, else it uses the fixed crop.

ffmpeg runs at below-normal priority on Windows (``nice`` elsewhere).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

_SCREEN_HINTS = ("screen", "display", "computer", "content", "desktop", "vga", "hdmi", "slides", "presentation")
_CAMERA_HINTS = ("camera", "cam", "ptz", "presenter", "lecturer", "video")
_HW_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")
_encoder_cache: dict[str, str] = {}


@dataclass
class PipSettings:
    enabled: bool = True
    size: float = 0.22
    position: str = "bottom-right"
    margin: float = 0.02
    encoder: str = "auto"
    crf: int = 23
    preset: str = "veryfast"


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(high, max(low, value))


def pip_settings() -> PipSettings:
    flag = os.environ.get("UNISYNC_LECTURE_VIDEO_PIP", "").strip().lower()
    position = os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_POSITION", "").strip().lower() or "bottom-right"
    if position not in {"bottom-right", "bottom-left", "top-right", "top-left"}:
        position = "bottom-right"
    crf_raw = os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_CRF", "").strip()
    return PipSettings(
        enabled=flag not in {"0", "false", "no", "off"},
        size=_float_env("UNISYNC_LECTURE_VIDEO_PIP_SIZE", 0.22, 0.05, 0.6),
        position=position,
        margin=_float_env("UNISYNC_LECTURE_VIDEO_PIP_MARGIN", 0.02, 0.0, 0.2),
        encoder=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_ENCODER", "").strip() or "auto",
        crf=int(crf_raw) if crf_raw.isdigit() else 23,
        preset=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_PRESET", "").strip() or "veryfast",
    )


def ffmpeg_threads() -> int:
    raw = os.environ.get("UNISYNC_LECTURE_VIDEO_FFMPEG_THREADS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 2


def low_priority_kwargs() -> dict[str, Any]:
    """Subprocess kwargs that keep ffmpeg from competing with the user's apps."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)}
    return {"preexec_fn": lambda: os.nice(10)}


def video_sources(payload_root: Mapping[str, Any]) -> list[int]:
    """Echo360 source indexes that carry video, in order (``[1, 2]`` for two feeds)."""
    playable = payload_root.get("playableAudioVideo")
    medias = playable.get("playableMedias") if isinstance(playable, Mapping) else None
    found: list[int] = []
    for item in medias or []:
        if not isinstance(item, Mapping):
            continue
        tracks = item.get("trackType") or []
        names = {str(t).lower() for t in ([tracks] if isinstance(tracks, str) else tracks)}
        index = item.get("sourceIndex")
        if "video" in names and isinstance(index, int) and index not in found:
            found.append(index)
        uri = str(item.get("uri") or "")
        match = re.search(r"/s(\d+)_(?:v|av)\.m3u8", uri)
        if "video" in names and match and int(match.group(1)) not in found:
            found.append(int(match.group(1)))
    return sorted(found)


def _label_text(payload_root: Mapping[str, Any], index: int) -> str:
    """Any name Echo360 gives a source (shape varies by tenant)."""
    texts: list[str] = []
    for key in ("sources", "sourceNames", "inputs", "streams"):
        block = payload_root.get(key)
        if isinstance(block, list):
            for i, entry in enumerate(block):
                if isinstance(entry, Mapping) and entry.get("sourceIndex", entry.get("index", i)) == index:
                    texts.extend(str(v) for v in entry.values() if isinstance(v, str))
                elif isinstance(entry, str) and i == index:
                    texts.append(entry)
        elif isinstance(block, Mapping):
            value = block.get(str(index), block.get(index))  # type: ignore[call-overload]
            if isinstance(value, str):
                texts.append(value)
    return " ".join(texts).lower()


def assign_roles(payload_root: Mapping[str, Any], sources: Iterable[int]) -> dict[str, int]:
    """Map ``screen``/``camera`` to source indexes.

    A source name wins when Echo360 provides one. Otherwise the lower index is
    the screen (Echo360 captures the display input first) and the next is the camera.
    """
    ordered = sorted(set(sources))
    if not ordered:
        return {}
    roles: dict[str, int] = {}
    for index in ordered:
        text = _label_text(payload_root, index)
        if not text:
            continue
        if "screen" not in roles and any(h in text for h in _SCREEN_HINTS):
            roles["screen"] = index
        elif "camera" not in roles and any(h in text for h in _CAMERA_HINTS):
            roles["camera"] = index
    rest = [i for i in ordered if i not in roles.values()]
    if "screen" not in roles and rest:
        roles["screen"] = rest.pop(0)
    if "camera" not in roles and rest:
        roles["camera"] = rest.pop(0)
    return roles


_SOURCE_IN_NAME = re.compile(r"(?:^|/)(?:hd|sd|s)(\d+)(?:q\d+|_av|_v)?\.(?:mp4|m4s|m3u8)$", re.IGNORECASE)


def source_of_url(url: str) -> int:
    """Echo360 source index in a CDN file name (``hd1.mp4`` -> 1, ``s2q1.m4s`` -> 2); 0 if unknown."""
    from urllib.parse import urlparse

    match = _SOURCE_IN_NAME.search(urlparse(url).path)
    return int(match.group(1)) if match else 0


def feed_file_names(source: int) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(muxed files, video-only files, audio files) to probe for one source."""
    muxed = (f"hd{source}.mp4", f"hd{source}.m4s", f"sd{source}.mp4", f"sd{source}.m4s")
    video = (f"s{source}q1.mp4", f"s{source}q1.m4s", f"s{source}q0.mp4", f"s{source}q0.m4s")
    audio = ("s0q1.mp4", "s0q1.m4s", "s0q0.mp4", "s0q0.m4s")
    return muxed, video, audio


def durations_match(a: float, b: float, tolerance: float = 0.1) -> bool:
    """Feeds of one lecture have (nearly) the same length; a pre-roll does not."""
    if not a or not b:
        return True
    return abs(a - b) <= max(30.0, tolerance * max(a, b))


def feed_path(directory: Path, stem: str, role: str) -> Path:
    return Path(directory) / f"{stem}.{role}.mp4"


def pip_path(directory: Path, stem: str) -> Path:
    return Path(directory) / f"{stem}.pip.mp4"


def probe_streams(path: Path) -> dict[str, Any]:
    """Width/height/fps of the first video stream and whether audio exists."""
    ffprobe = shutil.which("ffprobe")
    info: dict[str, Any] = {"width": 0, "height": 0, "fps": 25.0, "audio": False}
    if not ffprobe:
        return info
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        streams = json.loads(out.stdout or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return info
    for stream in streams:
        if stream.get("codec_type") == "video" and not info["width"]:
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            num, _, den = str(stream.get("avg_frame_rate") or "25/1").partition("/")
            try:
                info["fps"] = float(num) / float(den or 1) if float(den or 1) else 25.0
            except ValueError:
                pass
        if stream.get("codec_type") == "audio":
            info["audio"] = True
    return info


def _camera_chain(diameter: int, camera: Mapping[str, Any] | None) -> str:
    """Filter chain from the camera input to a ``diameter`` square, fixed or presenter-tracked."""
    if camera and camera.get("mode") == "tracked":
        # Zoom = a scale whose size the plan changes per command; pan = a fixed
        # diameter-sized crop. The camera is only scaled down as far as the circle needs.
        sw, sh, x, y = camera["first"]
        return (
            f"[1:v]sendcmd=f={camera['cmd']},"
            f"scale@zoom=w={sw}:h={sh}:flags=bilinear,"
            f"crop@pan=w={diameter}:h={diameter}:x={x}:y={y},format=yuva420p[cam]"
        )
    center = float((camera or {}).get("center", 0.5))
    center = min(1.0, max(0.0, center))
    return (
        f"[1:v]crop='min(iw,ih)':'min(iw,ih)':'max(0,min(iw-ow,{center:g}*iw-ow/2))':0,"
        f"scale={diameter}:{diameter},format=yuva420p[cam]"
    )


def circle_diameter(screen_width: int, settings: PipSettings) -> int:
    return max(32, int(round(screen_width * settings.size / 2)) * 2)


def pip_filter(screen_width: int, settings: PipSettings, camera: Mapping[str, Any] | None = None) -> str:
    """Screen full frame, camera cropped square, masked to a circle, placed in a corner.

    ``camera`` is ``{"mode": "fixed", "center": 0.5}`` or a tracked plan from ``camera_crop``.
    """
    diameter = circle_diameter(screen_width, settings)
    margin = int(round(screen_width * settings.margin))
    x = f"W-w-{margin}" if "right" in settings.position else str(margin)
    y = f"{margin}" if "top" in settings.position else f"H-h-{margin}"
    r = diameter / 2
    # Anti-aliased disc, computed once per second (the mask never changes).
    mask = (
        f"color=c=black:s={diameter}x{diameter}:r=1,format=gray,"
        f"geq=lum='255*clip({r}-hypot(X+0.5-{r}\\,Y+0.5-{r})+0.5\\,0\\,1)'[mask]"
    )
    return (
        f"{_camera_chain(diameter, camera)};"
        f"{mask};[cam][mask]alphamerge[disc];"
        f"[0:v][disc]overlay={x}:{y}:shortest=1:format=auto,format=yuv420p[v]"
    )


def _encoder_works(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.2",
             "-c:v", name, "-f", "null", "-"],
            capture_output=True, timeout=30,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pick_encoder(ffmpeg: str, preference: str = "auto") -> str:
    if preference and preference != "auto":
        return preference
    if ffmpeg in _encoder_cache:
        return _encoder_cache[ffmpeg]
    choice = "libx264"
    try:
        listed = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        listed = ""
    for name in _HW_ENCODERS:
        if re.search(rf"\b{name}\b", listed) and _encoder_works(ffmpeg, name):
            choice = name
            break
    _encoder_cache[ffmpeg] = choice
    return choice


def encoder_args(encoder: str, settings: PipSettings) -> list[str]:
    if encoder == "libx264":
        return ["-c:v", "libx264", "-crf", str(settings.crf), "-preset", settings.preset]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", str(settings.crf), "-b:v", "0"]
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-global_quality", str(settings.crf)]
    if encoder == "h264_amf":
        return ["-c:v", encoder, "-rc", "cqp", "-qp_i", str(settings.crf), "-qp_p", str(settings.crf)]
    return ["-c:v", encoder]


def pip_argv(ffmpeg: str, screen: Path, camera: Path, dest: Path, *, screen_width: int,
             settings: PipSettings, encoder: str, camera_crop: Mapping[str, Any] | None = None) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        # The thread cap covers decoding and filtering too, not only the encoder.
        "-threads", str(ffmpeg_threads()), "-i", str(screen),
        "-threads", str(ffmpeg_threads()), "-i", str(camera),
        "-filter_complex_threads", str(ffmpeg_threads()),
        "-filter_complex", pip_filter(screen_width, settings, camera_crop),
        "-map", "[v]", "-map", "0:a:0?",
        *encoder_args(encoder, settings),
        "-threads", str(ffmpeg_threads()),
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        str(dest),
    ]


async def camera_crop(camera: Path, dest: Path, diameter: int = 0) -> dict[str, Any]:
    """Fixed crop, or a presenter-tracking plan written as an ffmpeg ``sendcmd`` file beside ``dest``."""
    from unisync.services.sync import lecture_video_tracking as tracking

    tsettings = tracking.track_settings()
    fixed: dict[str, Any] = {"mode": "fixed", "center": tracking.fixed_center(tsettings.crop)}
    info = probe_streams(camera)
    if not info.get("width"):
        return {**fixed, "note": f"fixed {tsettings.crop} (camera unreadable)"}
    plan = await asyncio.to_thread(tracking.track_camera, camera, int(info["width"]), int(info["height"]), tsettings)
    if not plan.tracked:
        return {**fixed, "note": f"fixed {tsettings.crop} ({plan.note})"}
    # A plain file name: ffmpeg runs in dest's folder, so no path escaping inside the filter graph.
    name = f"pip-track-{os.getpid()}.cmd"
    diameter = diameter or circle_diameter(1920, pip_settings())
    (Path(dest).parent / name).write_text(tracking.sendcmd_text(plan, diameter), encoding="ascii")
    return {
        "mode": "tracked", "cmd": name, "cmd_path": str(Path(dest).parent / name),
        "first": tracking.render_values(plan.keys[0], plan.width, plan.height, diameter),
        "note": f"tracked ({plan.note})",
    }


async def build_pip(
    screen: Path, camera: Path, dest: Path, settings: PipSettings | None = None, notes: dict[str, str] | None = None
) -> tuple[Path | None, str]:
    """Write ``dest``; returns (path, encoder) or (None, reason). ``notes["crop"]`` says how the camera was framed."""
    settings = settings or pip_settings()
    if not settings.enabled:
        return None, "pip disabled"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, "pip needs ffmpeg"
    width = probe_streams(screen).get("width") or 1920
    encoder = pick_encoder(ffmpeg, settings.encoder)
    part = dest.with_name(dest.name + ".part.mp4")
    attempts = [encoder] if encoder == "libx264" else [encoder, "libx264"]
    crop = await camera_crop(camera, dest, circle_diameter(int(width), settings))
    if notes is not None:
        notes["crop"] = str(crop.get("note") or "")
    try:
        for name in attempts:
            argv = pip_argv(ffmpeg, Path(screen).resolve(), Path(camera).resolve(), part.resolve(),
                            screen_width=int(width), settings=settings, encoder=name, camera_crop=crop)
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                cwd=str(Path(dest).parent), **low_priority_kwargs()
            )
            await proc.wait()
            if proc.returncode == 0 and part.is_file() and part.stat().st_size > 0:
                part.replace(dest)
                return dest, name
            part.unlink(missing_ok=True)
    finally:
        if crop.get("cmd_path"):
            Path(crop["cmd_path"]).unlink(missing_ok=True)
    return None, "ffmpeg could not build the picture-in-picture file"
