"""Presenter tracking for the picture-in-picture camera circle.

The camera feed of a lecture is a wide shot of the room. A fixed centre crop
often shows the empty lectern while the lecturer stands at one side. This
module samples the camera feed every couple of seconds at low resolution,
finds people with a small CPU object detector (NanoDet-Plus through OpenCV's
DNN module), and plans a smoothed crop that follows whoever is presenting at
the front of the room, zooming in and out smoothly (like a video-call
"centre stage"). ffmpeg applies the plan with ``sendcmd``: a ``scale`` whose
size changes per command does the zoom, and a fixed-size ``crop`` (the circle's
diameter) does the pan. The camera is only ever scaled down to the size the
circle needs, so the encode stays one cheap pass.

Nothing here needs a GPU or a network service. OpenCV is optional: without it
(or without the model file) the plan falls back to the fixed crop.

Settings (environment):

* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK`` - ``0`` turns tracking off. Default on when OpenCV and the model are available.
* ``UNISYNC_LECTURE_VIDEO_PIP_CROP`` - fixed crop used when tracking is off or unsure:
  ``center`` (default), ``left``, ``right``, or the horizontal centre as a fraction (``0.25``).
* ``UNISYNC_LECTURE_VIDEO_PIP_ZOOM`` - tracked crop height as a share of the camera height (default 0.6;
  ``1`` keeps the full height). Several presenters ease it wider, up to the full height.
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_DEAD_ZONE`` - ignore presenter moves smaller than this share of the width (default 0.015).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_GLIDE`` - seconds the crop centre takes to close most of a gap (default 0.5).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_ZOOM_GLIDE`` - the same for the zoom (default 1.0).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_MAX_PAN`` - pan speed cap, share of the width per second (default 0.35).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_INTERVAL`` - seconds between samples (default 2).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_SECONDS`` - a person is followed only after being seen this long (default 4).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_IGNORE_BELOW`` - ignore people whose box starts below this share
  of the frame height, i.e. audience heads in the bottom rows (default 0.6).
* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_HEIGHT`` - ignore boxes shorter than this share of the frame height (default 0.15).
* ``UNISYNC_LECTURE_VIDEO_PIP_MODEL`` - path to the NanoDet-Plus ONNX file. Default
  ``<data>/models/object_detection_nanodet_2022nov.onnx``, fetched once from the OpenCV model zoo
  (Apache-2.0, checked by SHA-256) unless ``UNISYNC_LECTURE_VIDEO_PIP_MODEL_DOWNLOAD=0``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

MODEL_NAME = "object_detection_nanodet_2022nov.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/object_detection_nanodet/" + MODEL_NAME
)
MODEL_SHA256 = "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186"
_INPUT = 416
_REG_MAX = 7
_PERSON = 0
_OFF = {"0", "false", "no", "off"}


# --------------------------------------------------------------------------- settings


@dataclass
class TrackSettings:
    enabled: bool = True
    crop: str = "center"
    zoom: float = 0.6
    interval: float = 2.0
    min_seconds: float = 4.0
    ignore_below: float = 0.6
    min_height: float = 0.15
    min_score: float = 0.4
    drop_after: float = 6.0  # a track not seen for this long is forgotten
    lost_hold: float = 10.0  # keep the last framing this long before falling back
    max_pan: float = 0.35  # crop centre speed cap, share of frame width per second
    dead_zone: float = 0.015  # ignore target moves smaller than this share of the width
    glide: float = 0.5  # pan time constant, seconds
    zoom_glide: float = 1.0  # zoom time constant, seconds
    max_zoom: float = 0.35  # zoom speed cap, share of the camera height per second
    step: float = 1 / 15  # seconds between crop commands (only changes are written)
    model: str = ""
    download: bool = True


def _float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(high, max(low, value))


def track_settings() -> TrackSettings:
    return TrackSettings(
        enabled=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_TRACK", "").strip().lower() not in _OFF,
        crop=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_CROP", "").strip().lower() or "center",
        zoom=_float("UNISYNC_LECTURE_VIDEO_PIP_ZOOM", 0.6, 0.3, 1.0),
        interval=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_INTERVAL", 2.0, 0.5, 10.0),
        min_seconds=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_SECONDS", 4.0, 0.0, 60.0),
        ignore_below=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_IGNORE_BELOW", 0.6, 0.1, 1.0),
        min_height=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_MIN_HEIGHT", 0.15, 0.0, 0.9),
        dead_zone=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_DEAD_ZONE", 0.015, 0.0, 0.3),
        glide=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_GLIDE", 0.5, 0.05, 10.0),
        zoom_glide=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_ZOOM_GLIDE", 1.0, 0.05, 10.0),
        max_pan=_float("UNISYNC_LECTURE_VIDEO_PIP_TRACK_MAX_PAN", 0.35, 0.02, 5.0),
        model=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_MODEL", "").strip(),
        download=os.environ.get("UNISYNC_LECTURE_VIDEO_PIP_MODEL_DOWNLOAD", "").strip().lower() not in _OFF,
    )


def fixed_center(crop: str) -> float:
    """Horizontal crop centre (share of width) for the fixed setting."""
    named = {"center": 0.5, "centre": 0.5, "middle": 0.5, "left": 0.0, "right": 1.0}
    if crop in named:
        return named[crop]
    try:
        return min(1.0, max(0.0, float(crop)))
    except ValueError:
        return 0.5


# --------------------------------------------------------------------------- detections


@dataclass
class Box:
    """A person box in shares of the frame (0..1)."""

    x1: float
    y1: float
    x2: float
    y2: float
    score: float = 1.0

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def height(self) -> float:
        return self.y2 - self.y1


def presenters(boxes: Iterable[Box], settings: TrackSettings) -> list[Box]:
    """People who could be presenting: tall enough and not in the bottom (audience) rows."""
    keep = []
    for box in boxes:
        if box.score < settings.min_score:
            continue
        if box.y1 > settings.ignore_below:
            continue
        if box.height < settings.min_height:
            continue
        keep.append(box)
    return keep


@dataclass
class _Track:
    cx: float
    x1: float
    x2: float
    y1: float
    first: float
    last: float
    hits: int = 1


@dataclass
class Target:
    """What the crop should frame at one sample time. ``None`` fields mean fall back."""

    t: float
    cx: float | None = None
    span: float = 0.0  # width the people need, share of frame width
    top: float = 0.0  # highest head, share of frame height
    mode: str = "fallback"


class PresenterTracker:
    """Follows confirmed people near the front; ignores anyone seen for less than ``min_seconds``."""

    def __init__(self, settings: TrackSettings, match: float = 0.15) -> None:
        self.settings = settings
        self.match = match
        self.tracks: list[_Track] = []
        self._last: Target | None = None

    def update(self, t: float, boxes: Sequence[Box]) -> Target:
        s = self.settings
        free = list(self.tracks)
        for box in sorted(boxes, key=lambda b: -b.score):
            best = min(free, key=lambda tr: abs(tr.cx - box.cx), default=None)
            if best is not None and abs(best.cx - box.cx) <= self.match:
                free.remove(best)
                # Average out detector noise on small moves; follow real walks at once.
                w = 0.5 if abs(best.cx - box.cx) < 0.05 else 1.0
                best.x1 += w * (box.x1 - best.x1)
                best.x2 += w * (box.x2 - best.x2)
                best.y1 += w * (box.y1 - best.y1)
                best.cx = (best.x1 + best.x2) / 2
                best.last = t
                best.hits += 1
            else:
                self.tracks.append(_Track(box.cx, box.x1, box.x2, box.y1, first=t, last=t))
        self.tracks = [tr for tr in self.tracks if t - tr.last <= s.drop_after]
        confirmed = [
            tr for tr in self.tracks
            if tr.last >= t - s.interval * 1.01 and tr.hits >= 2 and tr.last - tr.first >= s.min_seconds - 1e-9
        ] if s.min_seconds > 0 else [tr for tr in self.tracks if tr.last == t]
        if confirmed:
            x1 = min(tr.x1 for tr in confirmed)
            x2 = max(tr.x2 for tr in confirmed)
            target = Target(
                t=t, cx=(x1 + x2) / 2, span=x2 - x1, top=min(tr.y1 for tr in confirmed),
                mode="group" if len(confirmed) > 1 else "presenter",
            )
            self._last = target
            return target
        if self._last is not None and t - self._last.t <= s.lost_hold:
            return Target(t=t, cx=self._last.cx, span=self._last.span, top=self._last.top, mode="hold")
        return Target(t=t)


# --------------------------------------------------------------------------- crop plan


@dataclass
class CropKey:
    """Square crop of the camera from ``t`` on, in camera pixels."""

    t: float
    x: float
    y: float
    side: float


@dataclass
class CropPlan:
    keys: list[CropKey] = field(default_factory=list)
    width: int = 0  # camera frame size
    height: int = 0
    tight: int = 0  # crop side for one presenter, px
    full: int = 0  # widest crop side (camera height), px
    note: str = ""
    tracked_share: float = 0.0  # share of samples that followed a person

    @property
    def tracked(self) -> bool:
        return bool(self.keys) and self.tracked_share > 0


def _even(value: float) -> int:
    return int(round(value / 2)) * 2


def _ease(current: float, goal: float, step: float, tau: float, cap: float) -> float:
    """Exponential approach (time constant ``tau``) with a speed cap; frame-rate independent."""
    import math

    move = (goal - current) * (1 - math.exp(-step / max(tau, 1e-6)))
    return current + max(-cap, min(cap, move))


def plan_crop(targets: Sequence[Target], width: int, height: int, settings: TrackSettings) -> CropPlan:
    """Turn per-sample targets into an eased crop path (centre and size) every ``settings.step`` seconds.

    One presenter: ``zoom`` x camera height, centred on them. Several: eased wider
    until they fit (up to the camera height). Nobody or too far apart: the fixed
    crop at full height. Centre and size glide; nothing jumps.
    """
    if not targets or width <= 0 or height <= 0:
        return CropPlan(note="no samples")
    full = float(min(width, height))
    tight = float(max(32, min(full, height * settings.zoom)))
    fallback_cx = fixed_center(settings.crop) * width

    def wanted(tg: Target) -> tuple[float, float, float]:
        """(centre x px, side px, top y px) the target asks for."""
        if tg.cx is None:
            return fallback_cx, full, 0.0
        need = tg.span * width * 1.15
        if need > full:  # people too far apart for one circle: fixed framing
            return fallback_cx, full, 0.0
        side = max(tight, need)
        return tg.cx * width, side, max(0.0, tg.top * height - 0.12 * side)

    keys: list[CropKey] = []
    cx, side, top = wanted(targets[0])
    goal = (cx, side, top)
    idx = 0
    t = targets[0].t
    end = targets[-1].t + settings.interval
    step = settings.step
    pan_cap = settings.max_pan * width * step
    zoom_cap = settings.max_zoom * height * step
    while t <= end + 1e-9:
        while idx + 1 < len(targets) and targets[idx + 1].t <= t + 1e-9:
            idx += 1
            new = wanted(targets[idx])
            # Dead zone: detector noise and small shuffles do not move the crop.
            if (abs(new[0] - goal[0]) > settings.dead_zone * width or abs(new[1] - goal[1]) > 0.02 * full
                    or abs(new[2] - goal[2]) > settings.dead_zone * height):
                goal = new
        cx = _ease(cx, goal[0], step, settings.glide, pan_cap)
        side = _ease(side, goal[1], step, settings.zoom_glide, zoom_cap)
        top = _ease(top, goal[2], step, settings.glide, pan_cap)
        s_px = min(full, max(32.0, side))
        x_px = min(width - s_px, max(0.0, cx - s_px / 2))
        y_px = min(height - s_px, max(0.0, top))
        key = CropKey(round(t, 3), round(x_px, 1), round(y_px, 1), round(s_px, 1))
        if not keys or abs(keys[-1].x - key.x) >= 1 or abs(keys[-1].y - key.y) >= 1 or abs(keys[-1].side - key.side) >= 1:
            keys.append(key)
        t += step
    followed = sum(1 for tg in targets if tg.cx is not None) / len(targets)
    return CropPlan(keys=keys, width=width, height=height, tight=int(tight), full=int(full),
                    tracked_share=followed, note=f"followed {followed:.0%} of samples")


def render_values(key: CropKey, width: int, height: int, diameter: int) -> tuple[int, int, int, int]:
    """(scaled width, scaled height, crop x, crop y) that show ``key`` in a ``diameter`` square.

    The camera is scaled by ``diameter / side`` (a downscale unless the crop is
    smaller than the circle), then a ``diameter`` square is cut out.
    """
    k = diameter / max(1.0, key.side)
    sw = max(diameter, _even(width * k))
    sh = max(diameter, _even(height * k))
    x = int(round(min(sw - diameter, max(0.0, key.x * k))))
    y = int(round(min(sh - diameter, max(0.0, key.y * k))))
    return sw, sh, x, y


def sendcmd_text(plan: CropPlan, diameter: int) -> str:
    """ffmpeg ``sendcmd`` script driving ``scale@zoom`` (zoom) and ``crop@pan`` (position)."""
    lines = []
    prev: tuple[int, int, int, int] | None = None
    for key in plan.keys:
        sw, sh, x, y = render_values(key, plan.width, plan.height, diameter)
        parts = []
        if prev is None or (sw, sh) != prev[:2]:
            parts += [f"scale@zoom w {sw}", f"scale@zoom h {sh}"]
        if prev is None or x != prev[2]:
            parts.append(f"crop@pan x {x}")
        if prev is None or y != prev[3]:
            parts.append(f"crop@pan y {y}")
        if parts:
            lines.append(f"{key.t:.3f} " + ", ".join(parts) + ";")
        prev = (sw, sh, x, y)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- detector


def detector_available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def _default_model_dir() -> Path:
    root = os.environ.get("UNISYNC_DATA_DIR", "").strip()
    if not root:
        try:
            from unisync.core.path_service import get_path_service

            root = str(get_path_service().get_user_root())
        except Exception:
            root = str(Path.home() / ".unisync")
    return Path(root) / "models"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_path(settings: TrackSettings) -> Path | None:
    """The detector model, fetched once from the OpenCV zoo when allowed."""
    if settings.model:
        path = Path(settings.model)
        return path if path.is_file() else None
    path = _default_model_dir() / MODEL_NAME
    if path.is_file():
        return path
    if not settings.download:
        return None
    try:
        import httpx

        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            with part.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        if _sha256(part) != MODEL_SHA256:
            part.unlink(missing_ok=True)
            logger.info("Presenter model checksum mismatch; tracking off")
            return None
        part.replace(path)
        return path
    except Exception as exc:  # network or disk problem: fixed crop instead
        logger.info("Presenter model not available (%s)", type(exc).__name__)
        return None


class PersonDetector:
    """NanoDet-Plus (COCO) through OpenCV DNN, person class only, one CPU thread."""

    def __init__(self, path: Path) -> None:
        import cv2
        import numpy as np

        cv2.setNumThreads(1)
        self.cv2, self.np = cv2, np
        self.net = cv2.dnn.readNet(str(path))
        self.mean = np.array([103.53, 116.28, 123.675], dtype=np.float32).reshape(1, 1, 3)
        self.std = np.array([57.375, 57.12, 58.395], dtype=np.float32).reshape(1, 1, 3)
        self._anchors: dict[int, Any] = {}

    def _anchor(self, stride: int) -> Any:
        if stride not in self._anchors:
            np = self.np
            n = _INPUT // stride
            xv, yv = np.meshgrid(np.arange(n) * stride, np.arange(n) * stride)
            self._anchors[stride] = np.column_stack((xv.ravel() + 0.5 * (stride - 1), yv.ravel() + 0.5 * (stride - 1)))
        return self._anchors[stride]

    def detect(self, frame: Any, min_score: float = 0.35) -> list[Box]:
        """People in a BGR frame whose width is ``_INPUT`` (letterboxed to a square here)."""
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        scale = _INPUT / max(h, w)
        if scale != 1:
            frame = cv2.resize(frame, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        nh, nw = frame.shape[:2]
        canvas = np.zeros((_INPUT, _INPUT, 3), dtype=np.uint8)
        canvas[:nh, :nw] = frame
        blob = cv2.dnn.blobFromImage((canvas.astype(np.float32) - self.mean) / self.std)
        self.net.setInput(blob)
        outs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        boxes, scores = [], []
        project = np.arange(_REG_MAX + 1, dtype=np.float32)
        # Output order and level count vary by OpenCV version: pair levels by size.
        flat = [o.reshape(-1, o.shape[-1]) for o in outs]
        reg_dim = 4 * (_REG_MAX + 1)
        classes = sorted((o for o in flat if o.shape[-1] != reg_dim), key=lambda o: -o.shape[0])
        regs = sorted((o for o in flat if o.shape[-1] == reg_dim), key=lambda o: -o.shape[0])
        for cls, reg in zip(classes, regs):
            side = int(round(cls.shape[0] ** 0.5))
            if side * side != cls.shape[0] or reg.shape[0] != cls.shape[0]:
                continue
            stride = _INPUT // side
            anchors = self._anchor(stride)
            person = cls[:, _PERSON]
            pick = person >= min_score
            if not pick.any():
                continue
            reg = reg.reshape(-1, 4, _REG_MAX + 1)[pick]
            e = np.exp(reg - reg.max(axis=2, keepdims=True))
            dist = (e / e.sum(axis=2, keepdims=True)) @ project * stride
            pts = anchors[pick]
            xyxy = np.column_stack((pts[:, 0] - dist[:, 0], pts[:, 1] - dist[:, 1], pts[:, 0] + dist[:, 2], pts[:, 1] + dist[:, 3]))
            boxes.append(xyxy)
            scores.append(person[pick])
        if not boxes:
            return []
        xyxy = np.concatenate(boxes)
        conf = np.concatenate(scores)
        wh = xyxy.copy()
        wh[:, 2:] -= wh[:, :2]
        keep = cv2.dnn.NMSBoxes(wh.tolist(), conf.tolist(), min_score, 0.5)
        out = []
        for i in np.array(keep).reshape(-1):
            x1, y1, x2, y2 = (xyxy[i] / scale).tolist()
            out.append(Box(max(0, x1) / w, max(0, y1) / h, min(w, x2) / w, min(h, y2) / h, float(conf[i])))
        return out


# --------------------------------------------------------------------------- sampling


def sample_frames(video: Path, interval: float, width: int = _INPUT) -> Iterator[tuple[float, Any]]:
    """Yield (seconds, BGR frame) every ``interval`` seconds, decoded at low priority with one thread."""
    import numpy as np

    from unisync.services.sync.lecture_video_feeds import low_priority_kwargs, probe_streams

    ffmpeg = shutil.which("ffmpeg")
    info = probe_streams(video)
    if not ffmpeg or not info.get("width"):
        return
    height = int(round(info["height"] * width / info["width"] / 2)) * 2
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-skip_loop_filter", "all", "-skip_frame", "bidir", "-i", str(video),
        "-an", "-vf", f"fps=1/{interval:g}:round=down,scale={width}:{height}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    size = width * height * 3
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **low_priority_kwargs())
    try:
        index = 0
        while True:
            buf = proc.stdout.read(size) if proc.stdout else b""
            if len(buf) < size:
                break
            yield index * interval, np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
            index += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.kill() if proc.poll() is None else None
        proc.wait()


def track_camera(camera: Path, width: int, height: int, settings: TrackSettings | None = None) -> CropPlan:
    """Plan the camera crop for one lecture. Falls back to the fixed crop when it cannot track."""
    settings = settings or track_settings()
    if not settings.enabled:
        return CropPlan(note="tracking off")
    if not detector_available():
        return CropPlan(note="tracking needs opencv-python-headless")
    path = model_path(settings)
    if path is None:
        return CropPlan(note="presenter model unavailable")
    try:
        detector = PersonDetector(path)
        tracker = PresenterTracker(settings)
        targets = [
            tracker.update(t, presenters(detector.detect(frame, settings.min_score), settings))
            for t, frame in sample_frames(camera, settings.interval)
        ]
    except Exception as exc:
        logger.info("Presenter tracking failed (%s); fixed crop", type(exc).__name__)
        return CropPlan(note="tracking failed")
    plan = plan_crop(targets, width, height, settings)
    if not plan.tracked:
        return CropPlan(note="no presenter found")
    return plan
