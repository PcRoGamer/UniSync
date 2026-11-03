#!/usr/bin/env python3
"""Generate docs/images/demo-dummy.gif — simulated course TUI + download."""
import os
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "images", "demo-dummy.gif")
W, H = 920, 540
BG = (13, 17, 23)
FG = (230, 237, 243)
GREEN = (63, 185, 80)
CYAN = (88, 166, 255)
YELLOW = (210, 153, 34)
MUTED = (139, 148, 158)
SEL_BG = (35, 55, 45)
SEL_FG = (100, 220, 140)
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def load_font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def base(title_extra=""):
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    font = load_font(15)
    draw.text((24, 18), "echo360-dlp — simulated demo (placeholder course names)", fill=MUTED, font=font)
    if title_extra:
        draw.text((24, 40), title_extra, fill=MUTED, font=load_font(12))
    return img, draw, font


def frame_cmd():
    img, draw, font = base()
    y = 70
    lines = [
        (CYAN, '$ ./run.sh --pick-course --chrome -P -a --transcript-only'),
        (FG, "> Echo360 Cloud platform detected"),
        (FG, "> Logged in — loading enrolled courses... Done!"),
        (MUTED, ""),
        (YELLOW, "Select course(s) to download (SPACE to mark, ENTER to continue):"),
    ]
    for color, text in lines:
        draw.text((24, y), text, fill=color, font=font)
        y += 24
    return img


def frame_tui(cursor=2, selected=None):
    if selected is None:
        selected = {0, 2}
    img, draw, font = base("Course TUI  ·  ↑↓ move  ·  SPACE toggle  ·  ENTER confirm")
    draw.text((24, 70), "Select course(s) to download (SPACE to mark, ENTER to continue):", fill=YELLOW, font=font)
    courses = [
        "COURSE101 - Introduction to Examples (2 streams)",
        "COURSE205 - Intermediate Topics (1 stream)",
        "COURSE310 - Advanced Workshop (3 streams)",
        "COURSE450 - Capstone Project (1 stream)",
    ]
    y = 110
    for i, name in enumerate(courses):
        marked = i in selected
        prefix = "* " if i == cursor else "  "
        check = "[x] " if marked else "[ ] "
        line = prefix + check + name
        if i == cursor:
            draw.rectangle([20, y - 4, W - 20, y + 22], fill=SEL_BG)
            draw.text((28, y), line, fill=SEL_FG, font=font)
        elif marked:
            draw.text((28, y), line, fill=GREEN, font=font)
        else:
            draw.text((28, y), line, fill=MUTED, font=font)
        y += 32
    draw.text((24, H - 40), "Marked: {} course(s)".format(len(selected)), fill=MUTED, font=font)
    return img


def frame_download(progress=None):
    img, draw, font = base()
    y = 70
    lines = [
        (GREEN, ">> Downloading all streams for COURSE101 - Introduction to Examples"),
        (MUTED, "------------------------------------------------------------"),
        (FG, "    Course: COURSE101 - Introduction to Examples"),
        (FG, "    Stream: 2026 Semester 1 Lectures"),
        (FG, "      Mode: transcripts only  ·  dual feeds (-a)"),
        (MUTED, "------------------------------------------------------------"),
        (YELLOW, 'Downloading transcript for "2026-04-08 - Lecture 02"'),
        (FG, "  > Downloading subtitles:"),
    ]
    for color, text in lines:
        draw.text((24, y), text, fill=color, font=font)
        y += 24
    if progress is not None:
        bar_x, bar_y, bar_w = 24, H - 56, W - 48
        draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 16], outline=MUTED)
        draw.rectangle([bar_x, bar_y, bar_x + int(bar_w * progress), bar_y + 16], fill=GREEN)
        draw.text((bar_x, bar_y - 22), "Lecture 02.vtt  {:.0f}%".format(progress * 100), fill=FG, font=font)
    else:
        draw.text((24, y), "  Subtitles saved: 2026-04-08 - Lecture 02.vtt", fill=GREEN, font=font)
        draw.text((24, y + 28), "Done!", fill=GREEN, font=font)
        draw.text((24, y + 60), ">> All downloads under _local/downloads/…/transcript/", fill=FG, font=font)
    return img


frames = []
# intro command
for _ in range(10):
    frames.append(frame_cmd())
# TUI cursor moves / selections
for cur in [0, 1, 2, 2, 3, 2]:
    for _ in range(5):
        frames.append(frame_tui(cursor=cur, selected={0, 2}))
# confirm flash
for _ in range(6):
    frames.append(frame_tui(cursor=2, selected={0, 2}))
# download progress
for p in (0.25, 0.55, 0.85):
    for _ in range(6):
        frames.append(frame_download(progress=p))
# done
for _ in range(14):
    frames.append(frame_download(progress=None))

os.makedirs(os.path.dirname(OUT), exist_ok=True)
frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=160, loop=0)
print("Wrote", OUT, "({} frames)".format(len(frames)))
