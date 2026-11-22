"""Presenter tracking for the picture-in-picture camera circle.

The camera feed of a lecture is a wide shot of the room. A fixed centre crop
often shows the empty lectern while the lecturer stands at one side. This
module samples the camera feed every couple of seconds at low resolution,
finds people with a small CPU object detector (NanoDet-Plus through OpenCV's
DNN module), and plans a smoothed crop that follows whoever is presenting at
the front of the room. ffmpeg then applies the plan with ``sendcmd`` on a
``crop`` filter, so the encode itself stays one pass.

Nothing here needs a GPU or a network service. OpenCV is optional: without it
(or without the model file) the plan falls back to the fixed crop.

Settings (environment):

* ``UNISYNC_LECTURE_VIDEO_PIP_TRACK`` - ``0`` turns tracking off. Default on when OpenCV and the model are available.
* ``UNISYNC_LECTURE_VIDEO_PIP_CROP`` - fixed crop used when tracking is off or unsure:
  ``center`` (default), ``left``, ``right``, or the horizontal centre as a fraction (``0.25``).
* ``UNISYNC_LECTURE_VIDEO_PIP_ZOOM`` - tracked crop height as a share of the camera height (default 0.6;
  ``1`` keeps the full height). Several presenters widen it up to the full height.
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
    max_pan: float = 0.12  # crop centre speed cap, share of frame width per second
    dead_zone: float = 0.04  # ignore target moves smaller than this share of the width
    step: float = 0.1  # seconds between crop commands (only changes are written)
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
                best.cx, best.x1, best.x2, best.y1 = box.cx, box.x1, box.x2, box.y1
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
    """Crop state from ``t`` on. Both crops follow the same centre; ``wide`` picks which one is shown."""

    t: float
    x_tight: int
    y_tight: int
    x_wide: int
    wide: bool


@dataclass
class CropPlan:
    keys: list[CropKey] = field(default_factory=list)
    tight: int = 0  # tracked crop side, px
    full: int = 0  # widest crop side (camera height), px
    note: str = ""
    tracked_share: float = 0.0  # share of samples that followed a person

    @property
    def tracked(self) -> bool:
        return bool(self.keys) and self.tracked_share > 0


def _even(value: float) -> int:
    return int(round(value / 2)) * 2


def plan_crop(targets: Sequence[Target], width: int, height: int, settings: TrackSettings) -> CropPlan:
    """Turn per-sample targets into smoothed crop keyframes (pixels) every ``settings.step`` seconds.

    Two crop sizes exist: ``tight`` (``zoom`` x camera height) follows one presenter,
    ``full`` (the camera height) fits several presenters or is the fixed fallback.
    ffmpeg cannot resize a crop mid-stream, so the size switches between the two
    while the centre glides.
    """
    if not targets or width <= 0 or height <= 0:
        return CropPlan(note="no samples")
    full = _even(min(width, height))
    tight = max(32, min(full, _even(height * settings.zoom)))
    fallback_cx = fixed_center(settings.crop) * width

    def wanted(tg: Target) -> tuple[float, bool, float]:
        """(centre x px, wide?, top y px) the target asks for."""
        if tg.cx is None:
            return fallback_cx, True, 0.0
        need = tg.span * width * 1.15
        if need > full:  # people too far apart for one circle: fixed framing
            return fallback_cx, True, 0.0
        wide = need > tight
        top = 0.0 if wide else max(0.0, tg.top * height - 0.12 * tight)
        return tg.cx * width, wide, top

    keys: list[CropKey] = []
    cx, wide, top = wanted(targets[0])
    goal = (cx, wide, top)
    idx = 0
    t = targets[0].t
    end = targets[-1].t + settings.interval
    max_move = settings.max_pan * width * settings.step
    while t <= end + 1e-9:
        while idx + 1 < len(targets) and targets[idx + 1].t <= t + 1e-9:
            idx += 1
            new = wanted(targets[idx])
            # Dead zone: small shuffles of the presenter do not move the crop.
            if (abs(new[0] - goal[0]) > settings.dead_zone * width or new[1] != goal[1]
                    or abs(new[2] - goal[2]) > settings.dead_zone * height):
                goal = new
        # Ease toward the goal with a speed cap: no jitter, no whip pans.
        cx += max(-max_move, min(max_move, (goal[0] - cx) * 0.35))
        top += max(-max_move, min(max_move, (goal[2] - top) * 0.35))
        wide = goal[1]
        key = CropKey(
            round(t, 3),
            _even(min(width - tight, max(0.0, cx - tight / 2))),
            _even(min(height - tight, max(0.0, top))),
            _even(min(width - full, max(0.0, cx - full / 2))),
            wide,
        )
        if not keys or (keys[-1].x_tight, keys[-1].y_tight, keys[-1].x_wide, keys[-1].wide) != (
            key.x_tight, key.y_tight, key.x_wide, key.wide
        ):
            keys.append(key)
        t += settings.step
    followed = sum(1 for tg in targets if tg.cx is not None) / len(targets)
    return CropPlan(keys=keys, tight=tight, full=full, tracked_share=followed,
                    note=f"followed {followed:.0%} of samples")


def sendcmd_text(plan: CropPlan) -> str:
    """ffmpeg ``sendcmd`` script for ``crop@tight``, ``crop@wide`` and ``streamselect@zoom``."""
    lines = []
    prev: CropKey | None = None
    for key in plan.keys:
        parts = []
        if prev is None or key.x_tight != prev.x_tight:
            parts.append(f"crop@tight x {key.x_tight}")
        if prev is None or key.y_tight != prev.y_tight:
            parts.append(f"crop@tight y {key.y_tight}")
        if prev is None or key.x_wide != prev.x_wide:
            parts.append(f"crop@wide x {key.x_wide}")
        if prev is None or key.wide != prev.wide:
            parts.append(f"streamselect@zoom map {1 if key.wide else 0}")
        if parts:
            lines.append(f"{key.t:.3f} " + ", ".join(parts) + ";")
        prev = key
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
