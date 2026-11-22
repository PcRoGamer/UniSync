"""Presenter tracking for the PiP camera circle: synthetic detections only (no model, no network)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from unisync.services.sync import lecture_video_feeds as feeds
from unisync.services.sync import lecture_video_tracking as tracking
from unisync.services.sync.lecture_video_tracking import Box, PresenterTracker, Target

LECTURER = Box(0.02, 0.25, 0.14, 0.95, 0.8)


def _settings(**over):
    s = tracking.TrackSettings()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _run(tracker, frames, interval=2.0):
    return [tracker.update(i * interval, boxes) for i, boxes in enumerate(frames)]


def test_audience_heads_and_small_boxes_are_ignored():
    s = _settings()
    head = Box(0.42, 0.78, 0.61, 0.99, 0.9)  # student in the bottom row
    tiny = Box(0.5, 0.3, 0.52, 0.38, 0.9)  # far away / poster
    weak = Box(0.3, 0.2, 0.4, 0.9, 0.2)
    assert tracking.presenters([LECTURER, head, tiny, weak], s) == [LECTURER]
    assert tracking.presenters([head], _settings(ignore_below=0.9)) == [head]


def test_person_seen_briefly_is_not_followed():
    s = _settings()
    walker = Box(0.5, 0.2, 0.6, 0.95)
    targets = _run(PresenterTracker(s), [[walker], [walker], [], [], [], []])
    assert all(t.cx is None for t in targets)  # 2 s of presence < 4 s


def test_presenter_followed_after_min_seconds_and_held_when_lost():
    s = _settings()
    frames = [[LECTURER]] * 5 + [[]] * 3 + [[]] * 6
    targets = _run(PresenterTracker(s), frames)
    assert targets[1].cx is None and targets[2].mode == "presenter"
    assert targets[2].cx == pytest.approx(LECTURER.cx)
    assert targets[6].mode == "hold"  # briefly lost (turned away): keep framing
    assert targets[-1].cx is None  # lost for longer than lost_hold: fall back


def test_walker_does_not_pull_crop_from_presenter():
    s = _settings()
    walker = Box(0.7, 0.2, 0.8, 0.95)
    frames = [[LECTURER]] * 4 + [[LECTURER, walker]] + [[LECTURER]] * 3
    targets = _run(PresenterTracker(s), frames)
    assert all(t.mode == "presenter" for t in targets[2:])
    assert all(t.cx == pytest.approx(LECTURER.cx) for t in targets[2:])


def test_two_presenters_widen_or_fall_back():
    s = _settings()
    guest = Box(0.30, 0.25, 0.40, 0.95)
    near = _run(PresenterTracker(s), [[LECTURER, guest]] * 4)[-1]
    assert near.mode == "group" and near.span == pytest.approx(0.38)
    plan = tracking.plan_crop([near] * 4, 1280, 720, s)
    assert plan.keys[-1].side == pytest.approx(0.38 * 1280 * 1.15, abs=1)  # wide enough for both
    far = Box(0.85, 0.25, 0.95, 0.95)
    apart = _run(PresenterTracker(s), [[LECTURER, far]] * 4)[-1]
    plan = tracking.plan_crop([apart] * 4, 1280, 720, _settings(crop="right"))
    assert plan.keys[-1].side == 720 and plan.keys[-1].x == 1280 - 720  # fixed fallback framing


def test_plan_is_smooth_and_capped():
    s = _settings()
    targets = [Target(t=i * 2.0, cx=0.1 if i < 5 else 0.9, span=0.1, top=0.25, mode="presenter") for i in range(15)]
    plan = tracking.plan_crop(targets, 1280, 720, s)
    xs = [k.x for k in plan.keys]
    cap = s.max_pan * 1280 * s.step + 1
    assert all(abs(b - a) <= cap for a, b in zip(xs, xs[1:]))
    assert xs[0] == 0 and xs[-1] == 1280 - plan.tight
    assert plan.tight == 432 and plan.full == 720


def test_jitter_inside_dead_zone_does_not_move_crop():
    s = _settings()
    import random

    rng = random.Random(1)
    targets = [Target(t=i * 2.0, cx=0.5 + rng.uniform(-0.007, 0.007), span=0.1, top=0.25, mode="presenter")
               for i in range(30)]
    plan = tracking.plan_crop(targets, 1280, 720, s)
    assert len({k.x for k in plan.keys}) == 1


def test_presenter_ends_up_centred_in_the_circle():
    s = _settings()
    targets = [Target(t=i * 2.0, cx=0.30 if i < 3 else 0.55, span=0.1, top=0.25, mode="presenter") for i in range(8)]
    plan = tracking.plan_crop(targets, 1280, 720, s)
    settled = [k for k in plan.keys if k.t <= 6 + 2.0][-1]  # 2 s after the presenter moved
    assert abs(settled.x + settled.side / 2 - 0.55 * 1280) <= 0.02 * 1280
    small_move = [Target(t=i * 2.0, cx=0.50 if i < 3 else 0.525, span=0.1, top=0.25, mode="presenter") for i in range(8)]
    keys = tracking.plan_crop(small_move, 1280, 720, s).keys
    assert keys[-1].x + keys[-1].side / 2 == pytest.approx(0.525 * 1280, abs=3)  # 2.5% move is followed


def test_zoom_eases_out_for_a_group_and_back_in():
    s = _settings()
    one = dict(cx=0.5, span=0.1, top=0.25, mode="presenter")
    group = dict(cx=0.5, span=0.45, top=0.25, mode="group")
    seq = [one] * 3 + [group] * 4 + [one] * 5
    targets = [Target(t=i * 2.0, **kw) for i, kw in enumerate(seq)]
    plan = tracking.plan_crop(targets, 1280, 720, s)
    sides = [k.side for k in plan.keys]
    cap = s.max_zoom * 720 * s.step + 1
    assert all(abs(b - a) <= cap for a, b in zip(sides, sides[1:]))  # no cut, only easing
    assert min(sides) == pytest.approx(432) and max(sides) == pytest.approx(min(720, 0.45 * 1280 * 1.15), abs=1)
    widest = sides.index(max(sides))
    assert sides[0] == pytest.approx(432) and sides[-1] == pytest.approx(432, abs=1)  # eased back in
    steps = [k.t for k in plan.keys if 432 < k.side < max(sides) - 1]
    assert steps and max(steps) - min(steps) > 1.0  # the transitions take time
    assert 0 < widest < len(sides) - 1


def test_render_values_scale_down_to_the_circle():
    key = tracking.CropKey(0.0, 0.0, 100.0, 432.0)
    sw, sh, x, y = tracking.render_values(key, 1280, 720, 282)
    assert (sw, sh) == (836, 470) and x == 0 and y == 65
    wide = tracking.render_values(tracking.CropKey(0.0, 560.0, 0.0, 720.0), 1280, 720, 282)
    assert wide[1] == 282 and abs(wide[2] - (wide[0] - 282)) <= 1


def test_fixed_center_parsing():
    assert tracking.fixed_center("left") == 0.0
    assert tracking.fixed_center("right") == 1.0
    assert tracking.fixed_center("0.25") == 0.25
    assert tracking.fixed_center("nonsense") == 0.5
    assert tracking.fixed_center("7") == 1.0


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_TRACK", "0")
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_CROP", "left")
    monkeypatch.setenv("UNISYNC_LECTURE_VIDEO_PIP_ZOOM", "5")
    s = tracking.track_settings()
    assert s.enabled is False and s.crop == "left" and s.zoom == 1.0


def test_sendcmd_writes_only_changes():
    plan = tracking.CropPlan(
        keys=[tracking.CropKey(0.0, 0.0, 100.0, 432.0), tracking.CropKey(0.1, 20.0, 100.0, 432.0),
              tracking.CropKey(0.2, 20.0, 100.0, 500.0)],
        width=1280, height=720, tight=432, full=720,
    )
    lines = tracking.sendcmd_text(plan, 282).strip().splitlines()
    assert lines[0] == "0.000 scale@zoom w 836, scale@zoom h 470, crop@pan x 0, crop@pan y 65;"
    assert lines[1] == "0.100 crop@pan x 13;"
    assert lines[2].startswith("0.200 scale@zoom w 722, scale@zoom h 406")


def test_track_camera_falls_back_without_detector(monkeypatch, tmp_path):
    monkeypatch.setattr(tracking, "detector_available", lambda: False)
    plan = tracking.track_camera(tmp_path / "cam.mp4", 1280, 720, _settings())
    assert not plan.tracked and "opencv" in plan.note
    assert tracking.track_camera(tmp_path / "cam.mp4", 1280, 720, _settings(enabled=False)).note == "tracking off"


def test_missing_model_without_download_is_none(tmp_path):
    assert tracking.model_path(_settings(model=str(tmp_path / "nope.onnx"))) is None


def test_filters_for_fixed_and_tracked_crop():
    s = feeds.PipSettings()
    fixed = feeds.pip_filter(1280, s, {"mode": "fixed", "center": 0.0})
    assert "max(0,min(iw-ow,0*iw-ow/2))" in fixed and "sendcmd" not in fixed
    tracked = feeds.pip_filter(1280, s, {"mode": "tracked", "cmd": "x.cmd", "first": (836, 470, 0, 65)})
    assert "sendcmd=f=x.cmd" in tracked and "scale@zoom=w=836:h=470" in tracked
    assert "crop@pan=w=282:h=282:x=0:y=65" in tracked and "streamselect" not in tracked


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_tracked_pip_renders_with_synthetic_plan(monkeypatch, tmp_path):
    for name, src in (("screen.mp4", "testsrc2"), ("camera.mp4", "testsrc")):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=s=640x360:r=10:d=3",
             "-f", "lavfi", "-i", "sine=d=3", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / name)],
            check=True,
        )
    targets = [Target(t=i * 1.0, cx=0.1 if i < 2 else 0.6, span=0.1 if i != 2 else 0.6, top=0.2, mode="presenter")
               for i in range(4)]
    plan = tracking.plan_crop(targets, 640, 360, _settings(interval=1.0))
    monkeypatch.setattr(tracking, "track_camera", lambda *a, **k: plan)
    notes: dict[str, str] = {}
    out, how = asyncio.run(feeds.build_pip(tmp_path / "screen.mp4", tmp_path / "camera.mp4", tmp_path / "x.pip.mp4",
                                           feeds.PipSettings(encoder="libx264"), notes=notes))
    assert out is not None and out.stat().st_size > 0 and how == "libx264"
    assert notes["crop"].startswith("tracked")
    assert not list(tmp_path.glob("pip-track-*.cmd"))  # plan file cleaned up
