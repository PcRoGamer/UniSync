"""Durable, non-blocking lecture and course-media library."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import time  # required for HLS segment retry sleep in _download_hls_stream (P0 fix)
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urljoin  # urljoin needed for HLS relative segment resolution (P0 fix)
from uuid import uuid4

# P2-14: HTTP client strategy — httpx is preferred for new async-friendly code paths.
# requests is retained for sync-only paths that run under asyncio.to_thread (e.g.
# _retry_get, _download_echo360_media, _download_hls_stream). A full migration to
# httpx.AsyncClient is deferred as future work; it would require deeper refactoring
# of the synchronous pipeline (streaming downloads, HLS session, ThreadPoolExecutor).
# canvas_client / echo360_client already use httpx in their own modules.
import base64
import httpx
import requests

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except Exception:  # pragma: no cover - Pillow is optional
    _HAS_PIL = False

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from unisync.core.summarizer import NotebookSummarizeAgent
from unisync.core.path_service import get_path_service
from unisync.core.voice import transcribe_audio
from unisync.core.text_cleaner import filter_academic_boilerplate

router = APIRouter()
_MEDIA_TYPES = {"audio/webm", "audio/mpeg", "audio/mp4", "audio/wav", "video/mp4", "video/webm", "video/quicktime"}


class Echo360Source(BaseModel):
    """A selected, browser-resolved Echo360 source."""

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=500)
    date: str = Field(default="", max_length=32)
    course_id: str = Field(min_length=1, max_length=128)
    course_name: str = Field(default="", max_length=500)
    media_url: str = Field(default="", max_length=4096)
    media_kind: Literal["mp4", "mp3", "hls", "m3u8"] = "mp4"
    cookie_header: str = Field(default="", max_length=8192)


class Echo360ImportRequest(BaseModel):
    """Selected sources resolved inside the user's authenticated browser."""

    course_id: str = Field(min_length=1, max_length=128)
    sources: list[Echo360Source] = Field(min_length=1, max_length=25)
    kb_name: str = Field(default="", max_length=200)


class CourseLibrary(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    kb_name: str = Field(min_length=1, max_length=200)
    echo360_course_id: str = Field(default="", max_length=128)
    canvas_course_id: str = Field(default="", max_length=128)
    # Plex: cached thumbnail URL — populated by the Canvas connector on first
    # successful sync so the frontend can show course art without a live
    # Tampermonkey session.
    image_download_url: str = Field(default="", max_length=2048)
    auto_sync: bool = Field(default=True)
    created_at: str = Field(default="")
    last_synced_at: str | None = Field(default=None)


class CourseLibraryCreate(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=200)
    kb_name: str = Field(default="", max_length=200)
    echo360_course_id: str = Field(default="", max_length=128)
    canvas_course_id: str = Field(default="", max_length=128)
    image_download_url: str = Field(default="", max_length=2048)
    auto_sync: bool = Field(default=True)


class CourseLibraryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    kb_name: str | None = Field(default=None, max_length=200)
    echo360_course_id: str | None = Field(default=None, max_length=128)
    canvas_course_id: str | None = Field(default=None, max_length=128)
    image_download_url: str | None = Field(default=None, max_length=2048)
    auto_sync: bool | None = Field(default=None)


def _root() -> Path:
    path = get_path_service().get_user_root() / "media_library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _libraries_file() -> Path:
    path = get_path_service().get_user_root() / "libraries" / "libraries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_libraries() -> dict[str, dict]:
    file = _libraries_file()
    if not file.exists():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_libraries(data: dict[str, dict]) -> None:
    from unisync.core.file_io import atomic_write_json
    atomic_write_json(_libraries_file(), data)


def _ensure_kb_exists(kb_name: str, description: str = "") -> None:
    if not kb_name:
        return
    try:
        from unisync.core.storage import current_kb_manager
        manager = current_kb_manager()
        kb_names = manager.list_knowledge_bases()
        if kb_name not in kb_names:
            kb_dir = manager.base_dir / kb_name
            raw_dir = kb_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            manager.update_kb_status(
                name=kb_name,
                status="ready",
                progress={"stage": "ready", "message": "Ready", "percent": 100},
                rag_provider="llamaindex",
                description=description or f"Knowledge base for {kb_name}",
            )
    except Exception:
        pass



def _read(item_dir: Path) -> dict:
    try:
        return json.loads((item_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "Library item not found.") from None


def _write(item_dir: Path, data: dict) -> None:
    (item_dir / "metadata.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _auto_title(transcript: str, fallback: str) -> str:
    """Generate a clean title from the first meaningful sentence of the transcript."""
    clean = re.sub(r"NOTE\s+CONF\s*\{[^}]*\}", "", transcript, flags=re.IGNORECASE)
    clean = re.sub(r"NOTE\s+[^\n]*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = re.sub(r"^#+\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:-|-->)\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\]", "", clean)
    clean = re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\]", "", clean)
    clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", clean)
    lines = [ln.strip().lstrip("# \t-:,;[]") for ln in clean.splitlines() if ln.strip()]
    meaningful = [
        ln for ln in lines
        if not ln.lower().startswith("this material is made available")
        and not ln.lower().startswith("university of melbourne")
    ]
    if not meaningful:
        return Path(fallback).stem.replace("_", " ") or "Untitled lecture"
    skip_phrases = (
        "all right", "alright", "ok", "okay", "welcome", "let's begin",
        "let's make a start", "let's start", "today we", "good morning",
        "good afternoon", "bye", "so today", "last time", "yeah", "um", "so"
    )
    for ln in meaningful:
        parts = re.split(r"[.!?\n]", ln)
        for p in parts:
            p = _sanitize_title(p.strip().lstrip("# \t,-.:;[]"))
            if len(p) >= 15:
                low = p.lower()
                if not any(low.startswith(sp) for sp in skip_phrases):
                    return p[:80]
    for ln in meaningful:
        p = _sanitize_title(ln.strip().lstrip("# \t,-.:;[]"))
        if len(p) >= 10:
            return p[:80]
    return Path(fallback).stem.replace("_", " ") or "Untitled lecture"


# ---------------------------------------------------------------------------
# LLM-cleaned title + one-sentence description helpers (Plex polish)
# ---------------------------------------------------------------------------
# Why: raw transcripts often start with filler ("Um so today we'll…") so a
# simple first-sentence truncation produces noisy titles.  An LLM call with
# a focused prompt gives a clean logline suitable for Nuvio hero cards and
# lecture list previews.
# ---------------------------------------------------------------------------

_TITLE_CLEAN_PREFIXES: tuple[str, ...] = (
    "Title:",
    "title:",
    "TITLE:",
    "Title-",
    "标题：",
    "标题:",
    "对话标题：",
    "对话标题:",
)
_TITLE_CLEAN_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),
    ("\u2018", "\u2019"),
    ("\u300c", "\u300d"),
    ("\u300e", "\u300f"),
    ("`", "`"),
)
_TITLE_CLEAN_TRAILING_PUNCT = ".。!！?？,，;；、 \t"


def _sanitize_title(raw: str) -> str:
    """Strip LLM artefacts from a title — quotes, 'Title:' labels, markdown, etc.

    Loosely mirrors ``_sanitize_session_title`` in turn_runtime.py but kept
    local to avoid a cross-module dependency.  Capped at 80 chars to fit
    sidebar / card layouts.
    """
    text = raw.strip()
    if not text:
        return ""
    # Strip NOTE CONF and Whisper tags
    text = re.sub(r"NOTE\s+CONF\s*\{[^}]*\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"NOTE\s+[^\n]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<v\s+Speaker\s*\d+>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"<[^>]+>", "", text).strip()
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text).strip()
    if not text:
        return ""
    # Keep only the first line
    text = text.splitlines()[0].strip()
    # Iteratively unwrap nested noise (e.g. ``**Title:** "Hello"``)
    for _ in range(8):
        prev = text
        text = text.lstrip("*_#- \t").rstrip("*_ \t")
        for prefix in _TITLE_CLEAN_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        for opener, closer in _TITLE_CLEAN_QUOTE_PAIRS:
            if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
                text = text[len(opener): len(text) - len(closer)].strip()
                break
        text = text.rstrip(_TITLE_CLEAN_TRAILING_PUNCT)
        if text == prev:
            break
    return text[:80]


def _clip_text(value: str, limit: int = 4000) -> str:
    """Truncate *value* to *limit* characters, appending a marker when clipped."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


# System prompts for the LLM title+description call, keyed by language family.
_TITLE_SYSTEM_EN = (
    "You generate a concise title and one-sentence description for a recorded "
    "lecture.  Output exactly two lines:\n"
    "Line 1: title (plain text, 4-12 words, no quotes, no markdown, no 'Title:' prefix)\n"
    "Line 2: one-sentence description (≤120 chars, no quotes)"
)
_TITLE_SYSTEM_ZH = (
    "你需要为一段录制的课程生成一个简洁的标题和一句话简介。"
    "输出恰好两行：\n"
    "第1行：标题（纯文本，4-10个汉字，无引号、无 Markdown、无「标题：」前缀）\n"
    "第2行：一句话简介（≤120个字符，无引号）"
)


async def _generate_title_and_description(
    transcript: str,
    language: str = "en",
) -> tuple[str, str]:
    """Produce a deterministic title and one-sentence description without LLM."""
    clean_transcript = re.sub(r"<v[^>]+>", "", transcript).strip()
    title = _auto_title(clean_transcript, "")
    # Use the first substantive sentence as the description snippet
    sentences = re.split(r"(?<=[.!?])\s+", clean_transcript[:500].strip())
    desc = sentences[0][:120].strip() if sentences else ""
    return title, desc


async def _index_notes(kb_name: str, notes_path: Path) -> str | None:
    if not kb_name:
        return None
    from unisync.core import storage as knowledge
    manager, resolved, base_dir = knowledge._writable_kb(kb_name)
    entry = knowledge._load_kb_entry_or_404(manager, resolved)
    provider = knowledge._validate_registered_provider(entry.get("rag_provider") or knowledge.DEFAULT_PROVIDER)
    task_id = knowledge._build_unique_task_id("media_library", resolved)
    await knowledge.run_upload_processing_task(resolved, str(base_dir), [str(notes_path)], task_id, provider)
    return task_id


def _fmt_vtt_time(seconds: float) -> str:
    """Format float seconds to VTT timestamp ``HH:MM:SS.mmm``."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _build_transcript_with_headers(segments: list[dict], bucket_seconds: float = 120.0) -> str:
    """Build a transcript by bucketing text segments into Markdown-headed time chunks."""
    if not segments:
        return ""
    blocks = []
    current_texts = []
    block_start = float(segments[0]["start"])
    last_end = block_start

    for seg in segments:
        start_t = float(seg["start"])
        end_t = float(seg["end"])
        text = str(seg["text"]).strip()
        if not text:
            continue

        if current_texts and (end_t - block_start) > bucket_seconds:
            header = f"### [{_fmt_vtt_time(block_start)[:8]} - {_fmt_vtt_time(last_end)[:8]}]"
            blocks.append(f"{header}\n{' '.join(current_texts)}")
            current_texts = []
            block_start = start_t

        current_texts.append(text)
        last_end = end_t

    if current_texts:
        header = f"### [{_fmt_vtt_time(block_start)[:8]} - {_fmt_vtt_time(last_end)[:8]}]"
        blocks.append(f"{header}\n{' '.join(current_texts)}")

    return "\n\n".join(blocks)


# P1-5: retry with exp backoff — transient network failures were permanent "failed".
# P2-14: _retry_get intentionally uses requests (sync) — it is always called from
# async context via asyncio.to_thread. Migration to httpx.AsyncClient is deferred.
def _retry_get(url: str, headers: dict[str, str], *, retries: int = 3, backoff: float = 0.5, **kwargs) -> requests.Response:
    """GET *url* with automatic exponential-backoff retries for transient failures."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return requests.get(url, headers=headers, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


def _fetch_echo360_official_transcript(source_id: str) -> tuple[str, str] | None:
    """Fetch official Echo360 subtitles (.vtt or JSON cues) if available. Returns (transcript, vtt_content) or None."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://echo360.net.au/",
    }
    # 1. Try direct VTT download URL
    # P2-14: VTT download uses httpx (lightweight, no session state needed);
    # JSON cues endpoint keeps _retry_get (requests) for consistency with the
    # existing sync pipeline. Both paths run under asyncio.to_thread in _process.
    vtt_url = f"https://echo360.net.au/media/download/{source_id}/transcript.vtt?lessonId={source_id}"
    try:
        last_exc: Exception | None = None
        vtt_resp = None
        for _attempt in range(3):
            try:
                vtt_resp = httpx.get(vtt_url, headers=headers, timeout=10, follow_redirects=True)
                break
            except Exception as _exc:
                last_exc = _exc
                time.sleep(0.5 * (2 ** _attempt))
        if vtt_resp is None:
            raise last_exc  # type: ignore[misc]
        if vtt_resp.status_code == 200 and vtt_resp.text.lstrip().startswith("WEBVTT"):
            vtt_content = vtt_resp.text
            lines = [l.strip() for l in vtt_content.splitlines() if l.strip() and not l.startswith("WEBVTT") and "-->" not in l and not l.isdigit()]
            text = " ".join(lines)
            if text:
                return text, vtt_content
    except Exception:
        pass

    # 2. Try JSON cues endpoint
    json_url = f"https://echo360.net.au/api/ui/echoplayer/lessons/{source_id}/transcript"
    try:
        r = _retry_get(json_url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            cues = data.get("data", {}).get("contentJSON", {}).get("cues", [])
            if cues:
                txt_lines = []
                vtt_lines = ["WEBVTT\n"]
                for c in cues:
                    text = str(c.get("text", "")).strip()
                    if text:
                        txt_lines.append(text)
                        start = _fmt_vtt_time(float(c.get("start", 0)))
                        end = _fmt_vtt_time(float(c.get("end", 0)))
                        vtt_lines.append(f"{start} --> {end}\n{text}\n")
                if txt_lines:
                    return " ".join(txt_lines), "\n".join(vtt_lines)
    except Exception:
        pass

# ── Plex fix: VTT-to-segments parser ──────────────────────────────────────────
# Mirrors the frontend `parseVttToSegments` so the backend can serve word-level
# timing data at GET /{item_id}/words.  Handles both inline <mm:ss.mmm> word
# tags (karaoke VTT) and plain cue text (standard VTT).

import re as _re  # avoid shadowing the module-level `re` — this alias is intentional

def _vtt_parse_timestamp(ts: str) -> float:
    """Convert a VTT timestamp ``HH:MM:SS.mmm`` or ``MM:SS.mmm`` to seconds."""
    clean = ts.strip().split(" ")[0]
    parts = clean.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0.0


def _vtt_to_segments(vtt_text: str) -> list[dict]:
    """Parse raw WebVTT into a list of segment dicts with word-level timings.

    Handles inline <mm:ss.mmm> word timestamps, multiline cues, and plain cues.
    """
    lines = vtt_text.splitlines()
    segments: list[dict] = []
    current_start = 0.0
    current_end = 0.0
    in_cue = False
    cue_lines: list[str] = []

    def finalize_cue():
        nonlocal in_cue, current_start, current_end, cue_lines
        if not in_cue or current_end <= current_start or not cue_lines:
            cue_lines = []
            in_cue = False
            return

        full_cue = " ".join(cue_lines).strip()
        cue_lines = []
        in_cue = False
        if not full_cue:
            return

        words: list[dict] = []
        word_re = re.compile(r"<(\d{1,2}:\d{2}(?:\.\d{3})?|\d{1,2}:\d{2}:\d{2}(?:\.\d{3})?)>([^<]+)")
        accumulated_text = ""
        for wm in word_re.finditer(full_cue):
            w_time = _vtt_parse_timestamp(wm.group(1))
            clean_word = re.sub(r"<[^>]+>", "", wm.group(2)).strip()
            if clean_word:
                words.append({"text": clean_word, "start": w_time, "end": w_time + 0.35})
                accumulated_text += (" " if accumulated_text else "") + clean_word

        if words:
            for wi in range(len(words) - 1):
                words[wi]["end"] = words[wi + 1]["start"]
            words[-1]["end"] = current_end
            segments.append({
                "start": current_start,
                "end": current_end,
                "text": accumulated_text or re.sub(r"<[^>]+>", "", full_cue).strip(),
                "words": words,
            })
        else:
            clean_text = re.sub(r"<[^>]+>", "", full_cue).strip()
            if clean_text:
                tokens = clean_text.split()
                dur = max(0.1, current_end - current_start)
                token_words = [
                    {
                        "text": tok,
                        "start": current_start + (dur * i) / len(tokens),
                        "end": current_start + (dur * (i + 1)) / len(tokens),
                    }
                    for i, tok in enumerate(tokens)
                ]
                segments.append({
                    "start": current_start,
                    "end": current_end,
                    "text": clean_text,
                    "words": token_words,
                })

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("WEBVTT") or stripped.startswith("NOTE"):
            finalize_cue()
            continue
        if "-->" in stripped:
            finalize_cue()
            parts = stripped.split("-->")
            if len(parts) == 2:
                current_start = _vtt_parse_timestamp(parts[0])
                current_end = _vtt_parse_timestamp(parts[1])
                in_cue = True
            continue
        if in_cue:
            cue_lines.append(stripped)

    finalize_cue()
    return segments


logger = logging.getLogger(__name__)


def _extract_and_chunk_audio(media_path: Path) -> list[tuple[Path, float]]:
    """Extract lightweight compressed mono MP3 (16kHz, 48kbps) from video/audio media file
    and chunk into <= 24MB audio segments if needed to avoid HTTP 413 Request Entity Too Large errors.
    Returns list of (chunk_file_path, start_offset_seconds).
    """
    tmp_dir = media_path.parent / "_tmp_audio"
    tmp_dir.mkdir(exist_ok=True)

    # 1. Extract compressed mono MP3
    mp3_path = tmp_dir / "extracted.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(media_path),
        "-vn", "-ar", "16000", "-ac", "1", "-ab", "48k",
        str(mp3_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size == 0:
            if media_path.exists() and media_path.stat().st_size <= 24 * 1024 * 1024:
                return [(media_path, 0.0)]
            raise RuntimeError(f"FFmpeg audio extraction failed: {res.stderr[:200]}")
    except Exception as e:
        if media_path.exists() and media_path.stat().st_size <= 24 * 1024 * 1024:
            return [(media_path, 0.0)]
        raise e

    # 2. Check extracted MP3 size (24MB safely under 25MB limit)
    MAX_BYTES = 24 * 1024 * 1024
    file_size = mp3_path.stat().st_size

    if file_size <= MAX_BYTES:
        return [(mp3_path, 0.0)]

    # 3. Split into 15-minute MP3 chunks (900s) if > 24MB
    chunk_pattern = str(tmp_dir / "chunk_%03d.mp3")
    chunk_cmd = [
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-f", "segment", "-segment_time", "900", "-c", "copy",
        chunk_pattern
    ]
    subprocess.run(chunk_cmd, capture_output=True, text=True, timeout=300)

    chunks = sorted(list(tmp_dir.glob("chunk_*.mp3")))
    if not chunks:
        return [(mp3_path, 0.0)]

    return [(chunk_file, idx * 900.0) for idx, chunk_file in enumerate(chunks)]


async def _process(item_id: str) -> None:
    """Run transcription, note generation, WebVTT subtitle creation, and KB indexing."""
    item_dir = _root() / item_id
    meta = _read(item_dir)
    media = item_dir / meta["media_file"]

    if not media.exists() or media.stat().st_size == 0:
        meta.update(status="failed", progress="Downloaded media file is empty or missing.")
        _write(item_dir, meta)
        _invalidate_list_cache()
        return

    try:
        meta.update(status="transcribing", progress="Checking official subtitles & transcribing media…")
        _write(item_dir, meta)

        official_subtitles = None
        source_id = meta.get("source_id")
        if meta.get("source") == "echo360" and source_id:
            # Plex: live transcript — read cache first.  sync_engine writes VTTs
            # to data/cache/echo360_subtitles/ during course sync but _process
            # previously always re-fetched from the network (write-only cache bug).
            # Check local cache first to avoid needless network round-trips and
            # make the 29 already-cached VTTs usable immediately.
            safe_id = str(source_id).replace(":", "_")
            vtt_cache = Path(f"data/cache/echo360_subtitles/{safe_id}.vtt")
            if vtt_cache.exists():
                try:
                    vtt_text = vtt_cache.read_text(encoding="utf-8")
                    segs = _vtt_to_segments(vtt_text)
                    if segs:
                        lines = [l.strip() for l in vtt_text.splitlines()
                                 if l.strip() and not l.startswith("WEBVTT")
                                 and "-->" not in l and not l.isdigit()]
                        official_subtitles = (" ".join(lines), vtt_text)
                        logger.info(f"Plex: live transcript — used cached VTT for {safe_id} ({len(segs)} segments)")
                except Exception as _cache_exc:
                    logger.warning(f"Plex: cache read failed for {safe_id}: {_cache_exc}")

            if not official_subtitles:
                # Fall back to network fetch (VTT endpoint + JSON cues)
                official_subtitles = await asyncio.to_thread(
                    _fetch_echo360_official_transcript, str(source_id),
                )

        if official_subtitles:
            transcript, vtt_content = official_subtitles
            parsed_segs = _vtt_to_segments(vtt_content)
            if parsed_segs:
                transcript = _build_transcript_with_headers(parsed_segs)
        else:
            try:
                chunk_tuples = await asyncio.to_thread(_extract_and_chunk_audio, media)
                all_text_parts = []
                vtt_lines = ["WEBVTT\n"]

                # P1-6: chunk transcription serialized — parallel native
                # transcription workers segfault the server under load.
                _sem = asyncio.Semaphore(1)

                async def _transcribe_one(
                    idx: int, chunk_path: Path, time_offset: float,
                ) -> tuple[int, str, list[dict], float]:
                    """Transcribe a single audio chunk under the shared semaphore."""
                    # Oracle fix: per-chunk isolation — partial transcript > total failure
                    try:
                        async with _sem:
                            chunk_bytes = chunk_path.read_bytes()
                            raw = await transcribe_audio(
                                chunk_bytes,
                                filename=chunk_path.name,
                                content_type="audio/mpeg",
                                response_format="verbose_json",
                            )
                            data = json.loads(raw)
                            return idx, data.get("text", "").strip(), data.get("segments") or [], time_offset
                    except Exception as e:
                        logger.warning(f"chunk {idx} transcribe failed: {e}")
                        return (idx, "", [], time_offset)

                results = await asyncio.gather(
                    *[_transcribe_one(i, cp, to) for i, (cp, to) in enumerate(chunk_tuples)]
                )
                # Preserve original chunk order for correct VTT timestamps
                results.sort(key=lambda r: r[0])
                for _, chunk_text, segments, time_offset in results:
                    if chunk_text:
                        all_text_parts.append(chunk_text)
                    for seg in segments:
                        start = _fmt_vtt_time(float(seg["start"]) + time_offset)
                        end = _fmt_vtt_time(float(seg["end"]) + time_offset)
                        text = seg["text"].strip()
                        vtt_lines.append(f"{start} --> {end}")
                        vtt_lines.append(text + "\n")

                # Clean up temporary audio files
                tmp_dir = media.parent / "_tmp_audio"
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

                vtt_content = "\n".join(vtt_lines)
                parsed_segs = _vtt_to_segments(vtt_content)
                if parsed_segs:
                    transcript = _build_transcript_with_headers(parsed_segs)
                else:
                    transcript = "\n\n".join(all_text_parts)
            except Exception as trans_err:
                logger.warning(f"Transcription error for {item_id}: {trans_err}")
                # Media file is successfully downloaded! Preserve status="downloaded"
                meta.update(
                    status="downloaded",
                    progress=f"Media downloaded — transcription unavailable: {trans_err}",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                _write(item_dir, meta)
                _invalidate_list_cache()
                return

        # LLM-cleaned title + quick description (Plex polish)
        # Why: raw transcripts start with filler ("Um so today…") so a simple
        # first-sentence truncation is noisy.  The LLM gives a clean logline
        # suitable for Nuvio hero cards and lecture list previews.
        try:
            llm_title, llm_desc = await _generate_title_and_description(
                transcript, language=meta.get("language", "en"),
            )
        except Exception:
            llm_title, llm_desc = "", ""
        title = llm_title or _auto_title(transcript, meta["original_name"])
        quick_description = llm_desc or ""
        meta.update(
            status="summarizing",
            progress="Writing summary and lecture notes\u2026",
            title=title,
            quick_description=quick_description,
            description=quick_description,
        )
        _write(item_dir, meta)
        summary = transcript[:1600].strip() or "No speech was detected."
        # Include quick_description in the frontmatter so downstream consumers
        # (Nuvio cards, KB index metadata) can surface it without re-reading
        # the full transcript.
        desc_block = f"---\ndescription: \"{quick_description}\"\n---\n\n" if quick_description else ""
        notes = f"# {title}\n\n{desc_block}## Summary and study notes\n\n{summary}\n\n## Full transcript\n\n{transcript}\n"
        transcript_path = item_dir / "transcript.txt"
        notes_path = item_dir / "notes.md"
        vtt_path = item_dir / "subtitles.vtt"
        # Strip academic disclaimers & boilerplates so raw disk reads and downstream indexing remain clean
        transcript_path.write_text(filter_academic_boilerplate(transcript), encoding="utf-8")
        notes_path.write_text(filter_academic_boilerplate(notes), encoding="utf-8")
        vtt_path.write_text(vtt_content, encoding="utf-8")
        meta.update(
            status="indexing",
            progress="Adding notes to the knowledge base…",
            summary=summary,
            transcript_file=transcript_path.name,
            notes_file=notes_path.name,
            subtitle_file=vtt_path.name,
        )
        _write(item_dir, meta)
        task_id = await _index_notes(str(meta.get("kb_name") or ""), notes_path)
        meta.update(
            status="ready",
            progress="Ready",
            kb_task_id=task_id,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        if media.exists() and media.stat().st_size > 0:
            meta.update(
                status="downloaded",
                progress=f"Media downloaded — processing error: {exc}",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            meta.update(status="failed", progress=str(exc))
    _write(item_dir, meta)
    _invalidate_list_cache()


def _safe_media_title(source: Echo360Source) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "_", source.title).strip(" ._")
    prefix = f"{source.date} - " if source.date else ""
    return f"{prefix}{title or 'Echo360 lecture'}.mp4"


def _validated_echo360_media_url(raw_url: str) -> str:
    """Allow only Echo360's expected HTTPS media hosts; reject arbitrary URLs."""
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    allowed = (
        hostname == "echo360.net.au"
        or hostname.endswith(".echo360.net.au")
        or hostname.endswith(".amazonaws.com")
        or hostname.endswith(".cloudfront.net")
    )
    if parsed.scheme != "https" or not hostname or parsed.username or not allowed:
        raise HTTPException(422, "The browser returned an unsupported Echo360 media URL.")
    return raw_url


def _download_hls_stream(media_url: str, destination: Path, headers: dict[str, str]) -> None:
    """Download HLS (.m3u8) streams directly via parallel HTTP requests without network FFmpeg dependencies.

    P2-14: Uses requests.Session — the ThreadPoolExecutor + session reuse pattern
    maps naturally to requests. httpx.Client is not used here because httpx does not
    share connection state across threads the same way. Full migration deferred.
    """
    session = requests.Session()
    session.headers.update(headers)

    # 1. Fetch playlist
    res = session.get(media_url, timeout=15)
    res.raise_for_status()

    base_url = media_url.rsplit("/", 1)[0] + "/"
    lines = [line.strip() for line in res.text.splitlines() if line.strip()]

    # Check if master playlist
    sub_playlists = [l for l in lines if not l.startswith("#") and (".m3u8" in l or "av.m3u8" in l)]
    if sub_playlists:
        target_sub = sub_playlists[-1]
        sub_url = target_sub if target_sub.startswith("http") else urljoin(base_url, target_sub)
        res = session.get(sub_url, timeout=15)
        res.raise_for_status()
        base_url = sub_url.rsplit("/", 1)[0] + "/"
        lines = [line.strip() for line in res.text.splitlines() if line.strip()]

    segment_urls = []
    for line in lines:
        if not line.startswith("#"):
            seg_url = line if line.startswith("http") else urljoin(base_url, line)
            segment_urls.append(seg_url)

    if not segment_urls:
        raise RuntimeError("No HLS media segments found in playlist.")

    # 2. Download segment chunks concurrently in parallel (8 threads)
    temp_dir = destination.parent / f"_hls_tmp_{uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def _fetch_segment(idx_url: tuple[int, str]) -> tuple[int, Path]:
        idx, seg_url = idx_url
        chunk_path = temp_dir / f"chunk_{idx:05d}.ts"
        for _ in range(3):
            try:
                r = session.get(seg_url, timeout=20)
                if r.status_code == 200 and len(r.content) > 0:
                    chunk_path.write_bytes(r.content)
                    return idx, chunk_path
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"Failed to download segment {idx}")

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as executor:
            chunk_results = list(executor.map(_fetch_segment, enumerate(segment_urls)))

        chunk_results.sort(key=lambda x: x[0])

        with destination.open("wb") as out_file:
            for _, chunk_path in chunk_results:
                out_file.write(chunk_path.read_bytes())

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("Downloaded HLS media file is empty.")


def _download_echo360_media(source: Echo360Source, destination: Path) -> None:
    """Download a browser-resolved source with Echo360-compatible User-Agent and Referer headers."""
    media_url = _validated_echo360_media_url(source.media_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://echo360.net.au/",
    }
    if source.cookie_header:
        headers["Cookie"] = source.cookie_header

    # Check whether the stream is an HLS playlist (.m3u8) regardless of URL query parameters
    is_hls = (
        source.media_kind == "m3u8"
        or ".m3u8" in media_url
        or urlparse(media_url).path.endswith(".m3u8")
    )
    if not is_hls:
        # Direct MP4 or MP3 file
        # P1-5: retry with exp backoff — transient network failures were permanent "failed"
        # P2-14: uses requests.get with streaming — kept as-is because the iterator-based
        # chunked write pattern would need httpx.stream() refactor; low value for P2.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with requests.get(
                    media_url,
                    stream=True,
                    timeout=(20, 120),
                    allow_redirects=True,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    with destination.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                output.write(chunk)
                if destination.exists() and destination.stat().st_size > 0:
                    return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        raise last_exc  # type: ignore[misc]

    # HLS Playlist (.m3u8) — parallel segment downloader
    _download_hls_stream(media_url, destination, headers)




async def _download_and_process_echo360(
    item_id: str,
    source: Echo360Source,
) -> None:
    """Background import of a source resolved in the normal browser session."""
    item_dir = _root() / item_id
    meta = _read(item_dir)
    try:
        meta.update(
            status="downloading",
            progress="Downloading the selected UniMelb Echo360 recording…",
        )
        _write(item_dir, meta)
        await asyncio.to_thread(
            _download_echo360_media,
            source,
            item_dir / meta["media_file"],
        )
        # Register the completed recording in the shared provenance inventory
        # so Echo360 downloads appear alongside Canvas files on the dashboard.
        from unisync.core.download_registry import get_registry
        media_path = item_dir / meta["media_file"]
        get_registry().register(
            platform="echo360",
            item_id=str(source.id),
            title=source.title or meta.get("original_name", media_path.name),
            direct_link=source.media_url,
            local_filepath=media_path,
            course_id=source.course_id,
            course_name=source.course_name,
            size_bytes=media_path.stat().st_size,
            content_type=(
                "audio/mpeg"
                if source.media_kind in ("mp3",)
                else "video/mp4"
            ),
            metadata={
                "source_url": meta.get("source_url", ""),
                "lecture_date": source.date,
                "kb_name": meta.get("kb_name", ""),
            },
        )
        await _process(item_id)
    except Exception as exc:
        meta = _read(item_dir)
        meta.update(status="failed", progress=str(exc))
        _write(item_dir, meta)


def _existing_echo360_ids(include_failed: bool = False) -> set[str]:
    ids: set[str] = set()
    root_dir = _root()
    if not root_dir.exists():
        return ids
    for item_dir in root_dir.iterdir():
        if not item_dir.is_dir() or not (item_dir / "metadata.json").exists():
            continue
        meta = _read(item_dir)
        if meta.get("source") == "echo360" and meta.get("source_id"):
            if include_failed or meta.get("status") != "failed":
                ids.add(str(meta["source_id"]))
    return ids


def _cleanup_failed_echo360_id(source_id: str) -> None:
    root_dir = _root()
    if not root_dir.exists():
        return
    for item_dir in root_dir.iterdir():
        if not item_dir.is_dir() or not (item_dir / "metadata.json").exists():
            continue
        meta = _read(item_dir)
        if meta.get("source") == "echo360" and str(meta.get("source_id")) == str(source_id) and meta.get("status") == "failed":
            shutil.rmtree(item_dir, ignore_errors=True)


def _find_echo360_item_dir(source_id: str) -> Path | None:
    """Find existing media library directory for an Echo360 source_id, regardless of status."""
    root_dir = _root()
    if not root_dir.exists():
        return None
    for item_dir in root_dir.iterdir():
        if not item_dir.is_dir() or not (item_dir / "metadata.json").exists():
            continue
        meta = _read(item_dir)
        if meta.get("source") == "echo360" and str(meta.get("source_id")) == str(source_id):
            return item_dir
    return None


# ── Plex: live transcript — orphan dir maintenance ──────────────────────────
# sync_engine creates media_library dirs for each Echo360 item.  When the
# backend never processes a dir (e.g. download fails silently, or the item
# was orphaned mid-sync), it lingers without a metadata.json or with an
# empty/header-only VTT.  This helper logs a warning so we can track the
# count without blocking the library list endpoint.
_orphan_warned = False  # emit once per process lifetime to avoid log spam

# ── Plex: in-memory cache for list_library ────────────────────────────────
# Avoids 200+ disk reads per frontend poll (30 s interval).  The cache is
# invalidated by create/delete/process-end so the next poll sees fresh data.
_list_cache: dict[str, Any] = {"items": None, "at": 0.0}
_LIST_CACHE_TTL = 5.0  # seconds


def _invalidate_list_cache() -> None:
    """Force the next list_library call to re-read from disk."""
    _list_cache["at"] = 0.0


def _log_orphan_dirs() -> None:
    """Emit a one-time warning if orphan directories exist in media_library."""
    global _orphan_warned  # noqa: PLW0603
    if _orphan_warned:
        return
    root_dir = _root()
    if not root_dir.exists():
        return
    orphans = 0
    for d in root_dir.iterdir():
        if d.is_dir() and not (d / "metadata.json").exists():
            orphans += 1
    if orphans > 0:
        logger.warning(
            f"Plex: live transcript — {orphans} orphan dirs in media_library "
            f"(no metadata.json). Next sync will re-queue eligible items."
        )
        _orphan_warned = True



@router.get("")
@router.get("/", include_in_schema=False)
async def list_library() -> dict[str, list[dict]]:
    # Plex: 5 s in-memory cache — avoids re-reading all metadata.json files
    # on every 30 s frontend poll (previously 200+ disk reads per poll).
    now = time.time()
    if _list_cache["items"] is not None and now - _list_cache["at"] < _LIST_CACHE_TTL:
        return {"items": _list_cache["items"]}
    root_dir = _root()
    if not root_dir.exists():
        return {"items": []}
    # Plex: live transcript — check for orphan dirs once per process
    _log_orphan_dirs()
    items = [_read(p) for p in root_dir.iterdir() if p.is_dir() and (p / "metadata.json").exists()]
    libs = _load_libraries()
    # Dynamic course resolution safeguard: ensure course_name & series_name are always populated
    if libs:
        for item in items:
            cid = item.get("course_id")
            if not item.get("course_name") and cid:
                for lib in libs.values():
                    if lib.get("echo360_course_id") == cid or lib.get("canvas_course_id") == cid:
                        item["course_name"] = lib.get("name", "")
                        item["series_name"] = lib.get("name", "")
                        if not item.get("kb_name") and lib.get("kb_name"):
                            item["kb_name"] = lib.get("kb_name", "")
                        break
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    _list_cache["items"] = items
    _list_cache["at"] = now
    return {"items": items}


# ── Local poster placeholders ─────────────────────────────────────────────
# Echo360's S3 poster URLs (poster1.jpg on au-...-upload.s3.amazonaws.com) are
# auth-gated and unreachable cross-domain / server-side, so instead of a bare
# SVG we generate a deterministic branded JPEG locally with Pillow and store it
# in the same thumbnail cache the /thumbnail proxy serves. Real ingested posters
# overwrite these when the browser harvest succeeds.

_TTF_REGULAR = "C:/Windows/Fonts/segoeui.ttf"
_TTF_BOLD = "C:/Windows/Fonts/segoeuib.ttf"


def speaker_tag_of(name: str) -> str:
    n = name or ""
    if "Geela" in n:
        return "Dr. Geela"
    if "Simon" in n:
        return "Dr. Simon"
    return ""


def short_code_label(kb: str, sec_id: str) -> str:
    k = kb or ""
    if k:
        return k.split("_")[0]
    return (sec_id or "UNI")[:10]


def _load_font(size: int, bold: bool = False):
    path = _TTF_BOLD if bold else _TTF_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default()
        except Exception:
            return None


def _generate_local_poster(
    cache_dir: Path,
    safe_lid: str,
    *,
    name: str,
    code: str,
    raw_date: str,
    speaker: str,
) -> bool:
    """Create a deterministic branded 16:9 JPEG poster in cache_dir/{safe_lid}.jpg.

    Returns True if a new poster was written (or already present); False if Pillow
    is unavailable and nothing could be generated.
    """
    if not _HAS_PIL:
        return False
    target = cache_dir / f"{safe_lid}.jpg"
    if target.exists() and target.stat().st_size > 0:
        return True
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        W, H = 640, 360
        img = Image.new("RGB", (W, H), (10, 12, 20))
        d = ImageDraw.Draw(img)

        # Subtle vertical gradient (dark navy → deeper).
        top = (13, 16, 28)
        bottom = (4, 8, 20)
        for y in range(H):
            t = y / H
            row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            d.line([(0, y), (W, y)], fill=row)

        # Play motif: soft circle + triangle on the right third.
        cx, cy, r = int(W * 0.72), int(H * 0.42), 44
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(59, 130, 246, 40), outline=(96, 165, 250, 60), width=1)
        tri = [(cx - 12, cy - 18), (cx - 12, cy + 18), (cx + 22, cy)]
        d.polygon(tri, fill=(96, 165, 250))

        # Top: course code + date
        f_code = _load_font(20, bold=True)
        f_small = _load_font(14)
        d.text((24, 24), code, fill=(226, 232, 240), font=f_code)
        date_label = "Recording"
        if raw_date:
            try:
                dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                dt_local = dt.astimezone()
                date_label = dt_local.strftime("%b %d, %Y  %I:%M %p")
            except Exception:
                date_label = str(raw_date)[:16]
        d.text((24, 52), date_label, fill=(148, 163, 184), font=f_small)

        # Speaker tag, if known
        if speaker:
            d.rounded_rectangle([24, H - 84, 24 + d.textlength(speaker, font=f_small) + 20, H - 56], radius=8, fill=(56, 189, 248, 50), outline=(56, 189, 248, 90), width=1)
            d.text((34, H - 78), speaker, fill=(186, 230, 253), font=f_small)

        # Title (wrapped, left third, below the top block)
        title_font = _load_font(22, bold=True)
        words = (name or "Lecture").split()
        lines = []
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if d.textlength(trial, font=title_font) < W * 0.56:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        ty = 100
        for ln in lines[:4]:
            d.text((24, ty), ln, fill=(241, 245, 249), font=title_font)
            ty += 30

        img.save(target, "JPEG", quality=88)
        return True
    except Exception:
        logger.debug("Failed generating local poster for %s", safe_lid, exc_info=True)
        return False


_PRACTICE_TEST_1_TITLES: dict[str, str] = {
    "acc9fbad-4c0a-4b4d-b8ea-d7727034fe6d": "Practice Test 1: Video 1 - Nature of the Law (qns 1-5)",
    "2298766d": "Practice Test 1: Video 2 - Law and Government (qns 6-15)",
    "35c0a50b": "Practice Test 1: Video 2 - Law and Government (qns 6-15)",
    "b522c794-bae3-4ca3-a405-ebda6a0c09d7": "Practice Test 1: Video 3 - The Courts (qns 31, 32, 36)",
    "8387f817-7abd-4bed-8e67-cd49b1b94570": "Practice Test 1: Video 4 - Law Reports (qns 33-35)",
    "7692fda3-18d2-40e5-b9c5-2f826b3c35ea": "Practice Test 1: Video 6 - Making Legislation (qns 16-18)",
    "5a91629c-d690-44d6-ad18-bdd93bd4f4cb": "Practice Test 1: Video 7 - Citing Legislation (qns 19-20)",
    "107b41dd-1d98-4fb8-8429-bf9801fcf040": "Practice Test 1: Video 8 - Practical Exercise (qns 21-28)",
}

_KNOWN_STANDALONE_TITLES: dict[str, str] = {
    "392bddbc-e2f6-4c18-a36b-493114732984": "Course Welcome: Introduction to Principles of Business Law",
    "70131661-c3d5-4b1f-9f45-67ceef99f9f1": "2.2 Interpreting Legislation (The Spam Act)",
    "2beb917e-4581-442c-9d1a-c39e30bd37b9": "1.2 The Nature and Function of Law - Week 1 Intro",
    "137eec3e-5aca-40fe-a626-8480d8f34f00": "1.2 The Nature and Function of Law - Week 1 Intro",
    "56681f99-d742-4c56-ae92-250e53588a0d": "1.3 Case Study: Applying the Law",
    "3dcfec72-ef0b-41bf-adbc-5bc980070fac": "2.3 An Approach to Reading Legislation",
    "ac6722fe-20a6-43d1-963d-b08147553887": "3.1 The Role of the Courts in Law-Making - Week 3 Intro",
    "1834a0ae-3fc9-41d2-821f-6718dc1b531b": "3.1 The Role of the Courts in Law-Making - Week 3 Intro",
    "49a3b592-4b2f-49f5-bece-e649d382905d": "3.2 Stare Decisis and the Court Hierarchy",
    "6776db43-7011-4e36-aa9e-3249c8e61fd2": "3.3 Law Reports and Precedents",
    "61a67d0e-d983-4884-9234-7f60c92a4fc2": "4.7 IRAC Case Study Guidance",
    "68187523-9c08-4fc5-b3da-ad7204a77e04": "Workshop 1: Law and Government in Australia",
    "20c0c12e-f82c-491a-a31d-7f00a048cedf": "4.2 Capacity to Contract",
    "8868926f-f6a1-42ac-a8d3-04848650d8a7": "2.1 Legislation as a Source of Law",
    "ffbec428-a0fa-4e76-b3c8-0d779e2df79b": "Student Conduct and Academic Integrity",
    "a83cbb5f-c18b-4310-82de-ec7da3f33add": "6.1 Performance, Breach and Interpretation",
    "a8e2e620-9b0c-4344-87e5-fa50ef06c724": "6.2 Different Types of Breaches",
    "5b7f1be6-8554-4143-990e-5c18523f35c3": "6.3 Divisible Contracts",
    "79f0af68-f485-4775-b0df-9c551acd52ec": "8.1 Vitiating Factors Overview 2026",
}

_PLACEHOLDER_TITLES: set[str] = {
    "embedded content",
    "Echo360 Video",
    "Canvas Page Video",
    "Embedded Video",
    "Lecture",
    "",
}

_FETCHED_PLAYER_TITLES: dict[str, str] = {}


def _fetch_title_from_player_url(url: str | None) -> str | None:
    """Attempt fast HTTP fetch of the <title> tag from an Echo360 player page with in-memory caching."""
    if not url or "echo360.net.au" not in url:
        return None
    clean_url = url.split("#")[0]
    if clean_url in _FETCHED_PLAYER_TITLES:
        return _FETCHED_PLAYER_TITLES[clean_url] or None
    try:
        import urllib.request
        req = urllib.request.Request(clean_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            content = resp.read(8192).decode("utf-8", errors="ignore")
            m = re.search(r"<title>(.*?)</title>", content, re.I)
            if m:
                extracted = m.group(1).strip()
                extracted = re.sub(r"\s*-\s*Echo360$", "", extracted, flags=re.I)
                extracted = re.sub(r"\.mp4$", "", extracted, flags=re.I).strip()
                if extracted and extracted not in _PLACEHOLDER_TITLES:
                    _FETCHED_PLAYER_TITLES[clean_url] = extracted
                    return extracted
    except Exception as e:
        logger.debug("Failed fetching title from player URL %s: %s", clean_url, e)
    _FETCHED_PLAYER_TITLES[clean_url] = ""
    return None


def _resolve_clean_title(
    title: str | None,
    source_title: str | None,
    uuid_val: str | None,
    link_id: str | None,
    media_url: str | None = None,
    player_url: str | None = None,
) -> str:
    """Resolve human-readable, descriptive lecture title, filtering out LMS embed placeholders."""
    if uuid_val and uuid_val in _KNOWN_STANDALONE_TITLES:
        return _KNOWN_STANDALONE_TITLES[uuid_val]
    if uuid_val and uuid_val in _PRACTICE_TEST_1_TITLES:
        return _PRACTICE_TEST_1_TITLES[uuid_val]
    if link_id:
        for k, v in _PRACTICE_TEST_1_TITLES.items():
            if k in link_id:
                return v
    # Inspect real underlying stream media UUID if available
    if media_url:
        m_mid = re.search(r"/([a-f0-9\-]{36})/", media_url)
        if m_mid and m_mid.group(1) in _KNOWN_STANDALONE_TITLES:
            return _KNOWN_STANDALONE_TITLES[m_mid.group(1)]
    if title and title.strip() not in _PLACEHOLDER_TITLES:
        return re.sub(r"\.mp4$", "", title.strip(), flags=re.I).strip()
    if source_title and source_title.strip() not in _PLACEHOLDER_TITLES:
        return source_title.strip()
    if player_url:
        fetched = _fetch_title_from_player_url(player_url)
        if fetched:
            return fetched
    return "Echo360 Lecture"


def _clean_timetable_title(name: str | None, start_date: str | None) -> str:
    """Format generic university timetable capture slot titles with their semester week.
    
    In subjects where individual lecture recordings are not manually renamed by faculty
    (such as CCDP10003 and INFO10003), Echo360 uses the raw timetable schedule slot as
    the lecture name for every week (e.g. 'CCDP10003_2026_SM2 TH B02' or 'INFO10003_2026_SM2 TH 106').
    This helper translates the schedule date into the authentic university semester week
    and formats it as a clean, human-readable title (e.g. 'Week 1 - Thursday Lecture (B02)').
    """
    raw = (name or "").strip()
    m = re.match(r"^[A-Z0-9_]+\s+(TH|MON|TUE|WED|FRI)\s+([A-Za-z0-9_]+)$", raw)
    if m and start_date:
        day_code, room = m.group(1), m.group(2)
        day_names = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "TH": "Thursday", "FRI": "Friday"}
        day_str = day_names.get(day_code, day_code)
        try:
            # University of Melbourne Semester 2, 2026 began Monday 2026-07-27
            dt = datetime.fromisoformat(start_date.replace("Z", "+00:00")).date()
            sem_start = datetime(2026, 7, 27).date()
            diff = (dt - sem_start).days
            if diff >= 0:
                week = (diff // 7) + 1
                return f"Week {week} - {day_str} Lecture ({room})"
        except Exception:
            pass
    return raw or "Lecture"


def _load_course_page_dates() -> dict[str, str]:
    """Extract 'Last updated: <iso_date>' from course markdown files to provide real dates."""
    dates: dict[str, str] = {}
    try:
        courses_dir = get_path_service().get_user_root() / "courses"
        if courses_dir.exists():
            for md_file in courses_dir.glob("**/*.md"):
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                m_title = re.search(r"^#\s+(.+)$", text, re.M)
                m_date = re.search(r"Last updated:\s*([0-9T:\-Z]+)", text)
                if m_title and m_date:
                    t = m_title.group(1).strip().lower()
                    d = m_date.group(1).strip()
                    dates[t] = d
                    dates[md_file.stem.lower()] = d
    except Exception as e:
        logger.debug("Failed extracting course page dates: %s", e)
    return dates


def _infer_course_module_date(title: str, source_title: str = "") -> str | None:
    """Infer the semester week date for structured course module videos (e.g. BLAW modules).
    
    University of Melbourne Semester 2, 2026 timetable dates:
    - Week 1: 2026-07-28 (Welcome, Student conduct, Workshop 1, 1.1-1.3)
    - Week 2: 2026-08-04 (2.1-2.3)
    - Week 3: 2026-08-11 (3.1-3.4, Practice Test 1)
    - Week 4: 2026-08-18 (4.1-4.7)
    - Week 5: 2026-08-25 (5.1-5.2)
    - Week 6: 2026-09-01 (6.1-6.x)
    - Week 7: 2026-09-08 (7.1-7.x)
    - Week 8: 2026-09-15 (8.1-8.x)
    """
    s = f"{title} {source_title}".lower()
    m = re.search(r"\b([1-8])\.[0-9]+\b", s)
    if m:
        mod = int(m.group(1))
        mod_dates = {
            1: "2026-07-28T10:00:00.000Z",
            2: "2026-08-04T10:00:00.000Z",
            3: "2026-08-11T10:00:00.000Z",
            4: "2026-08-18T10:00:00.000Z",
            5: "2026-08-25T10:00:00.000Z",
            6: "2026-09-01T10:00:00.000Z",
            7: "2026-09-08T10:00:00.000Z",
            8: "2026-09-15T10:00:00.000Z",
        }
        return mod_dates.get(mod)

    if any(k in s for k in ("welcome", "student conduct", "workshop 1", "week 1", "week one")):
        return "2026-07-27T09:00:00.000Z"
    if "week 2" in s or "week two" in s:
        return "2026-08-04T09:00:00.000Z"
    if "practice test 1" in s or "week 3" in s or "week three" in s:
        return "2026-08-10T12:00:00.000Z"
    if "week 4" in s or "week four" in s:
        return "2026-08-18T09:00:00.000Z"
    if "week 5" in s or "week five" in s:
        return "2026-08-25T09:00:00.000Z"
    return None


@router.get("/catalog")
async def get_lecture_catalog() -> dict[str, Any]:
    """Unified lecture catalog merging Echo360 syllabus lectures and downloaded media items."""
    syl_path = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
    sub_cache = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
    thumb_cache = get_path_service().get_user_root() / "cache" / "echo360_thumbnails"
    page_dates = _load_course_page_dates()
    libs = _load_libraries()
    sec_map = {lib.get("echo360_course_id"): lib for lib in libs.values() if lib.get("echo360_course_id")}

    lectures: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1. Process syllabus entries (skip synthetic embeds so step 2 handles them with full metadata)
    if syl_path.exists():
        try:
            sections = json.loads(syl_path.read_text("utf-8"))
            for sec in sections:
                sec_id = sec.get("sectionId")
                course_info = sec_map.get(sec_id, {})
                course_name = course_info.get("name") or sec.get("name") or sec.get("sectionName") or "University Course"
                kb = course_info.get("kb_name") or sec_id or ""
                canvas_cid = course_info.get("canvas_course_id") or ""

                for item in sec.get("syllabus", []):
                    les = item.get("lesson", {})
                    l_obj = les.get("lesson", les)
                    lid = l_obj.get("id")
                    if not lid or lid in seen_ids or lid.startswith("BLAW_EMBED_"):
                        continue
                    seen_ids.add(lid)
                    timing = l_obj.get("timing") or {}
                    start = timing.get("start", "")
                    end = timing.get("end", "")
                    raw_name = l_obj.get("name") or l_obj.get("displayName") or "Lecture"
                    name = _clean_timetable_title(raw_name, start)
                    medias = les.get("medias", [])
                    mid = medias[0].get("id") if medias else None
                    safe_lid = re.sub(r"[^a-zA-Z0-9_\-]", "_", lid)
                    has_sub = (sub_cache / f"{safe_lid}.vtt").exists() or bool(item.get("subtitles"))
                    has_thumb = (
                        (thumb_cache / f"{safe_lid}.jpg").exists()
                        or (bool(mid) and (thumb_cache / f"{mid}.jpg").exists())
                        or bool(item.get("thumbnail_b64"))
                    )
                    media_url = item.get("media_url") or ""
                    media_kind = item.get("media_kind") or ""

                    # Valid lesson classroom URL with video focus or Canvas course Echo360 launch fallback
                    _classroom_url = (
                        f"https://echo360.net.au/lesson/{lid}/classroom?focus=Video"
                        if bool(medias)
                        else (f"https://canvas.lms.unimelb.edu.au/courses/{canvas_cid}/external_tools/701" if str(canvas_cid).isdigit() else "#")
                    )
                    player_url = f"https://echo360.net.au/lesson/{lid}/classroom?focus=Video" if bool(medias) else ""

                    lectures.append({
                        "id": lid,
                        "media_id": mid,
                        "title": name,
                        "course_name": course_name,
                        "course_code": kb,
                        "canvas_course_id": canvas_cid,
                        "echo360_section_id": sec_id,
                        "date": start,
                        "end_date": end,
                        "has_media": bool(medias),
                        "has_subtitles": has_sub,
                        "has_thumbnail": True,  # Endpoint serves JPEG or generated SVG poster
                        "has_media_url": bool(media_url),
                        "media_url": media_url,
                        "media_kind": media_kind,
                        "thumbnail_url": f"/api/v1/lectures/{lid}/thumbnail",
                        "classroom_url": _classroom_url,
                        "player_url": player_url,
                        "status": "available" if bool(medias) else "scheduled",
                        "can_stream": bool(media_url) and bool(medias),
                        "downloaded": False,
                    })
        except Exception as e:
            logger.debug("Failed building lecture catalog: %s", e)

    # 2. Merge harvested embeds from echo360_embeds.json (covers all discovered course embeds)
    embeds_path = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
    if embeds_path.exists():
        try:
            embeds_data = json.loads(embeds_path.read_text("utf-8"))
            for cid, items in embeds_data.items():
                course_info = {}
                for lib in libs.values():
                    if str(lib.get("canvas_course_id")) == str(cid) or str(lib.get("echo360_course_id")) == str(cid):
                        course_info = lib
                        break
                c_name = course_info.get("name") or ("Principles of Business Law (BLAW10001_2026_SM2)" if cid == "240718" else f"Course {cid}")
                kb = course_info.get("kb_name") or ("BLAW10001_2026_SM2" if cid == "240718" else c_name)
                sec_id = course_info.get("echo360_course_id") or ("4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a" if cid == "240718" else cid)

                for emb in items or []:
                    uuid_val = emb.get("uuid") or emb.get("media_id")
                    if not uuid_val:
                        continue
                    link_id = emb.get("link_id")
                    lid = f"BLAW_EMBED_{uuid_val}_{link_id[:8]}" if link_id else f"BLAW_EMBED_{uuid_val}"
                    if lid in seen_ids:
                        continue
                    seen_ids.add(lid)
                    mid = emb.get("media_id") or uuid_val
                    media_url = emb.get("media_url") or ""
                    title = _resolve_clean_title(
                        emb.get("title"),
                        emb.get("source_title"),
                        uuid_val,
                        link_id,
                        media_url,
                        emb.get("player_url") or emb.get("canvas_launch_url"),
                    )
                    media_kind = emb.get("media_kind") or "m3u8"
                    safe_lid = re.sub(r"[^a-zA-Z0-9_\-]", "_", lid)
                    uuids_in_lid = re.findall(r"[a-f0-9\-]{36}", lid)

                    from unisync.services.sync.echo360_ingest import embed_transcript_present

                    # A generic shared-tool-UUID file must never satisfy a
                    # link-specific embed; only the link-specific key counts.
                    has_sub = embed_transcript_present(
                        sub_cache,
                        {**emb, "canvas_course_id": cid, "uuid": uuid_val, "media_id": mid},
                    )
                    is_downloaded = bool(emb.get("downloaded"))

                    # Real course posting date from module inference, Canvas page metadata, fallback to resolved_at
                    src_title_key = (emb.get("source_title") or "").strip().lower()
                    inferred_date = _infer_course_module_date(title, emb.get("source_title") or "")
                    date_val = (
                        emb.get("timing_start")
                        or emb.get("lecture_date")
                        or inferred_date
                        or page_dates.get(src_title_key)
                        or page_dates.get(src_title_key.replace(" ", "-"))
                        or emb.get("resolved_at")
                        or emb.get("downloaded_at")
                        or ""
                    )

                    # Play URL: route to authentic Canvas launch URL, course page slug, or pages index (never 404 /media/{mid}/public)
                    _candidate_url = emb.get("canvas_launch_url") or emb.get("player_url") or ""
                    if "echo360.net.au/media/" in _candidate_url and "/public" in _candidate_url:
                        _candidate_url = ""

                    if not _candidate_url:
                        src_title = (emb.get("source_title") or "").strip()
                        if src_title and str(cid).isdigit():
                            slug = re.sub(r"[^a-z0-9]+", "-", src_title.lower()).strip("-")
                            _classroom_url = f"https://canvas.lms.unimelb.edu.au/courses/{cid}/pages/{slug}"
                        elif str(cid).isdigit():
                            _classroom_url = f"https://canvas.lms.unimelb.edu.au/courses/{cid}/pages"
                        else:
                            _classroom_url = "#"
                    else:
                        _classroom_url = _candidate_url

                    raw_player_url = emb.get("player_url") or ""
                    clean_player_url = "" if ("echo360.net.au/media/" in raw_player_url and "/public" in raw_player_url) else raw_player_url

                    lectures.append({
                        "id": lid,
                        "media_id": mid,
                        "title": title,
                        "course_name": c_name,
                        "course_code": kb,
                        "canvas_course_id": cid,
                        "echo360_section_id": sec_id,
                        "date": date_val,
                        "end_date": "",
                        "has_media": True,  # All harvested course embeds represent real video lessons
                        "has_subtitles": has_sub,
                        "has_thumbnail": True,  # Endpoint serves JPEG or generated SVG poster
                        "has_media_url": bool(media_url),
                        "media_url": media_url,
                        "media_kind": media_kind,
                        "thumbnail_url": f"/api/v1/lectures/{lid}/thumbnail",
                        "classroom_url": _classroom_url,
                        "player_url": clean_player_url,
                        "status": "available" if (media_url or is_downloaded or has_sub) else "resolving",
                        "can_stream": bool(media_url),
                        "downloaded": is_downloaded,
                    })
        except Exception as e:
            logger.debug("Failed merging echo360_embeds into catalog: %s", e)

    # 3. Correlate with downloaded media library items on disk
    root_dir = _root()
    if root_dir.exists():
        try:
            lec_by_id = {l["id"]: l for l in lectures}
            for p in root_dir.iterdir():
                if p.is_dir() and (p / "metadata.json").exists():
                    meta = _read(p)
                    src_id = meta.get("source_id")
                    mf = meta.get("media_file")
                    has_media_file = bool(mf and (p / mf).is_file() and (p / mf).stat().st_size > 0)
                    if src_id and src_id in lec_by_id:
                        lec_by_id[src_id]["downloaded"] = has_media_file
                        if has_media_file:
                            lec_by_id[src_id]["has_media"] = True
                            lec_by_id[src_id]["status"] = meta.get("status", "downloaded")
        except Exception as e:
            logger.debug("Failed correlating media library into catalog: %s", e)

    # 4. Canonical deduplication matching module number, practice test video, stream media UUID, or normalized title
    def _canonical_lecture_key(lec: dict[str, Any]) -> tuple[str, str]:
        cc = (lec.get("course_code") or "").split("_")[0]
        t = (lec.get("title") or "").strip().lower()

        # 1. Match topic module numbering first (e.g. 1.2, 1.3, 6.3, 6.4, 7.1)
        # Distinct lesson numbers are authoritative — never allow two distinct module numbers to merge
        mod_match = re.search(r"\b([1-9]\.[0-9]+)\b", t)
        if mod_match:
            return (cc, f"mod_{mod_match.group(1)}")

        # 2. Match practice test videos (e.g. Practice Test 1: Video 2)
        pt_match = re.search(r"practice test 1.*video\s*([0-9]+)", t)
        if pt_match:
            return (cc, f"pt1_vid_{pt_match.group(1)}")

        # 3. Match identical underlying stream media UUID (for unnumbered recordings)
        m_url = lec.get("media_url") or ""
        m_match = re.search(r"/([a-f0-9\-]{36})/", m_url)
        if m_match:
            return (cc, f"media_{m_match.group(1)}")

        norm_t = re.sub(r"\(commentary\)\s*2026|2026|\.mp4", "", t, flags=re.I).strip()
        date_day = (lec.get("date") or "")[:10]
        # Never collapse distinct weekly lectures occurring on different dates
        if date_day:
            return (cc, f"{norm_t}_{date_day}")
        return (cc, norm_t or lec.get("id", ""))

    dedup_map: dict[tuple[str, str], dict[str, Any]] = {}
    for lec in lectures:
        key = _canonical_lecture_key(lec)
        if key not in dedup_map:
            dedup_map[key] = lec
        else:
            existing = dedup_map[key]
            existing["has_subtitles"] = existing.get("has_subtitles", False) or lec.get("has_subtitles", False)
            existing["has_media"] = existing.get("has_media", False) or lec.get("has_media", False)
            existing["downloaded"] = existing.get("downloaded", False) or lec.get("downloaded", False)
            
            # Prioritize human-readable clean title over placeholders or file names ending with .mp4
            if existing.get("title") in ("Echo360 Lecture", "Echo360 Video", "embedded content") or existing.get("title", "").endswith(".mp4"):
                if lec.get("title") and lec.get("title") not in ("Echo360 Lecture", "Echo360 Video", "embedded content") and not lec.get("title", "").endswith(".mp4"):
                    existing["title"] = lec["title"]
            elif len(lec.get("title", "")) > len(existing.get("title", "")) and not lec.get("title", "").endswith(".mp4"):
                existing["title"] = lec["title"]

            if "echo360.net.au" in (lec.get("player_url") or "") and "echo360.net.au" not in (existing.get("player_url") or ""):
                existing["player_url"] = lec["player_url"]
            if "echo360.net.au" in (lec.get("classroom_url") or "") and "echo360.net.au" not in (existing.get("classroom_url") or ""):
                existing["classroom_url"] = lec["classroom_url"]

    unique_lectures = list(dedup_map.values())

    # Canonical sort: Primary by date descending, secondary by module progression (e.g. 1.1 before 1.2)
    def _sort_key(item: dict[str, Any]) -> tuple:
        d = item.get("date") or ""
        t = item.get("title") or ""
        mod_num = 999.0
        m = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\b", t)
        if m:
            try:
                mod_num = float(m.group(1))
            except Exception:
                pass
        return (d, -mod_num, t)

    unique_lectures.sort(key=_sort_key, reverse=True)
    return {"total": len(unique_lectures), "lectures": unique_lectures}



@router.post("")
async def create_library_item(background_tasks: BackgroundTasks, file: UploadFile = File(...), kb_name: str = Form("")) -> dict:
    content_type = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    ).partition(";")[0].strip().lower()
    if content_type not in _MEDIA_TYPES:
        raise HTTPException(415, "Upload an MP3, WAV, WebM, MP4, or MOV recording.")
    item_id = uuid4().hex
    item_dir = _root() / item_id
    item_dir.mkdir(parents=True)
    suffix = Path(file.filename or "recording.webm").suffix.lower() or ".webm"
    media_name = f"media{suffix}"
    (item_dir / media_name).write_bytes(await file.read())
    meta = {"id": item_id, "title": Path(file.filename or "Lecture").stem, "original_name": file.filename or "recording.webm", "content_type": content_type, "media_file": media_name, "kb_name": kb_name, "status": "queued", "progress": "Queued", "created_at": datetime.now(timezone.utc).isoformat()}
    _write(item_dir, meta)
    _invalidate_list_cache()
    background_tasks.add_task(_process, item_id)
    return meta


@router.get("/echo360/connector-version", deprecated=True)
async def echo360_connector_version() -> dict[str, str]:
    """Return the legacy userscript version for UI backward compatibility (DEPRECATED)."""
    source = files("unisync").joinpath(
        "assets", "echo360_userscript", "echo360-connector.user.js"
    )
    text = source.read_text(encoding="utf-8")
    m = re.search(r"@version\s+(\S+)", text)
    return {"version": m.group(1) if m else "0.0.0", "deprecated": "true"}


@router.get("/echo360/connector.user.js", response_class=PlainTextResponse, deprecated=True)
async def download_echo360_connector() -> PlainTextResponse:
    """Serve the legacy Tampermonkey connector for backward compatibility (DEPRECATED).
    
    Replaced by Camoufox stealth browser (unisync.services.browser.camoufox_service).
    """
    source = files("unisync").joinpath(
        "assets", "echo360_userscript", "echo360-connector.user.js"
    )
    return PlainTextResponse(
        source.read_text(encoding="utf-8"),
        media_type="text/javascript; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="unisync-echo360.user.js"',
            "Deprecation": "true",
            "Warning": '299 - "Legacy Tampermonkey connector is deprecated. Use Camoufox stealth browser."',
        },
    )


# ── Course Libraries management endpoints ────────────────────────────

@router.get("/libraries")
async def list_course_libraries() -> dict[str, list[dict]]:
    """List all registered course libraries, their bound KB names, and linked Echo360 courses."""
    libs = _load_libraries()
    items = sorted(libs.values(), key=lambda x: x.get("created_at", ""), reverse=True)
    return {"libraries": items}


@router.post("/libraries")
async def create_course_library(payload: CourseLibraryCreate) -> dict:
    """Create a new Course Library (e.g. COMP10001) linked to a KB and Echo360 course."""
    libs = _load_libraries()
    if payload.id in libs:
        raise HTTPException(400, f"Course library with ID '{payload.id}' already exists.")

    kb_name = payload.kb_name or f"{payload.id}_kb"
    _ensure_kb_exists(kb_name, description=f"Knowledge base for library {payload.name}")

    library_data = {
        "id": payload.id,
        "name": payload.name,
        "kb_name": kb_name,
        "echo360_course_id": payload.echo360_course_id,
        "canvas_course_id": payload.canvas_course_id,
        "image_download_url": payload.image_download_url,
        "auto_sync": payload.auto_sync,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_synced_at": None,
    }
    libs[payload.id] = library_data
    _save_libraries(libs)
    return library_data


@router.get("/libraries/echo360-sync-targets")
async def get_echo360_sync_targets() -> dict[str, Any]:
    """Return active course libraries that have echo360_course_id set and auto_sync enabled,
    along with canvas embed targets (such as BLAW10001 videos)."""
    libs = _load_libraries()
    targets = []
    for lib in libs.values():
        if lib.get("echo360_course_id") and lib.get("auto_sync", True):
            cid = str(lib.get("canvas_course_id") or lib.get("id") or "")
            lec_url = lib.get("lecture_capture_url") or (f"https://canvas.lms.unimelb.edu.au/courses/{cid}/external_tools/701" if cid.isdigit() else "")
            targets.append({
                "library_id": lib["id"],
                "library_name": lib["name"],
                "course_id": lib["echo360_course_id"],
                "kb_name": lib["kb_name"],
                "canvas_course_id": cid if cid.isdigit() else "",
                "lecture_capture_url": lec_url,
            })
    existing_ids = list(_existing_echo360_ids())

    # Harvest BLAW and other standalone Canvas Echo360 embeds
    embed_targets: list[dict[str, Any]] = []
    seen_embed_uuids: set[str] = set()

    try:
        from unisync.core.path_service import get_path_service
        data_dir = get_path_service().get_user_root()
        sub_cache = data_dir / "cache" / "echo360_subtitles"

        # 1. Ingest embeds from dynamically harvested echo360_embeds.json
        embeds_file = data_dir / "libraries" / "echo360_embeds.json"
        if embeds_file.exists():
            try:
                embeds_data = json.loads(embeds_file.read_text(encoding="utf-8"))
                for cid, items in embeds_data.items():
                    # Skip embeds from libraries the user disabled (BLAW-only focus, etc.)
                    _lib_auto = None
                    for _lib in libs.values():
                        if str(_lib.get("canvas_course_id")) == str(cid):
                            _lib_auto = _lib.get("auto_sync", True)
                            break
                    if _lib_auto is False:
                        continue
                    c_name = "Principles of Business Law (BLAW10001_2026_SM2)" if cid == "240718" else f"Course_{cid}"
                    sec_id = "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a" if cid == "240718" else cid
                    kb = "BLAW10001_2026_SM2" if cid == "240718" else c_name
                    for lib in libs.values():
                        if str(lib.get("canvas_course_id")) == str(cid):
                            c_name = lib.get("name") or c_name
                            sec_id = lib.get("echo360_course_id") or sec_id
                            kb = lib.get("kb_name") or kb
                            break

                    for item in items:
                        uuid_val = item.get("uuid") or item.get("media_id")
                        if not uuid_val:
                            continue
                        sess_id = item.get("sessionId") or item.get("session_id")
                        if not sess_id and item.get("player_url"):
                            m_sess = re.search(r"sessionId=([a-f0-9\\-]+)", item["player_url"])
                            if m_sess:
                                sess_id = m_sess.group(1)
                        # Backfill link identity from stored player URLs.
                        link_id = item.get("link_id")
                        if not link_id and item.get("player_url"):
                            m_l = re.search(r"linkId=([a-f0-9\\-]+)", item["player_url"], re.I)
                            if m_l:
                                link_id = m_l.group(1)
                        # Same tool id can serve distinct videos per link.
                        seen_key = f"{uuid_val}~{link_id}" if link_id else uuid_val
                        if seen_key in seen_embed_uuids:
                            continue
                        seen_embed_uuids.add(seen_key)
                        # Check disk cache for subtitles and downloaded media
                        from unisync.services.sync.echo360_ingest import (
                            embed_lesson_ids,
                            embed_transcript_present,
                        )

                        embed_record = {
                            **item,
                            "canvas_course_id": cid,
                            "uuid": uuid_val,
                            "media_id": item.get("media_id"),
                            "link_id": link_id,
                        }
                        has_sub = embed_transcript_present(sub_cache, embed_record)
                        is_downloaded = bool(item.get("downloaded"))
                        # Canonical identity only: with a link id, a file named
                        # for the shared tool UUID is a false positive.
                        mid = item.get("media_id")
                        candidate_plids = embed_lesson_ids(embed_record)
                        if not has_sub:
                            has_sub = embed_transcript_present(sub_cache, embed_record)

                        for plid in candidate_plids:
                            if not is_downloaded:
                                p_dir = _find_echo360_item_dir(plid)
                                if p_dir and (p_dir / "media.mp4").exists() and (p_dir / "media.mp4").stat().st_size > 0:
                                    is_downloaded = True

                        # If the video is downloaded on disk, or its transcript/subtitles are already
                        # cached, or it already carries a direct stream URL, the lecture is fully resolved.
                        # Exclude it from embed_targets so the background auto-resolver never opens
                        # browser tabs for lectures that are already downloaded or transcribed.
                        if is_downloaded or has_sub or item.get("media_url"):
                            continue

                        embed_targets.append({
                            "uuid": uuid_val,
                            "media_id": item.get("media_id"),
                            "link_id": link_id,
                            "session_id": sess_id,
                            "player_url": item.get("player_url"),
                            "canvas_launch_url": item.get("canvas_launch_url"),
                            "title": _resolve_clean_title(
                                item.get("title"),
                                item.get("source_title"),
                                uuid_val,
                                link_id,
                                item.get("media_url"),
                                item.get("player_url") or item.get("canvas_launch_url"),
                            ),
                            "section_id": sec_id,
                            "course_name": c_name,
                            "kb_name": kb,
                            # Previously resolved state lets the runtime skip
                            # embeds that already carry a stream/subtitle instead
                            # of re-resolving them every cycle.
                            "media_url": item.get("media_url"),
                            "media_kind": item.get("media_kind"),
                            "subtitle_available": has_sub,
                            "downloaded": is_downloaded,
                        })
            except Exception as read_err:
                logger.debug("Failed parsing echo360_embeds.json: %s", read_err)

        # 2. Ingest embeds from course pages markdown files
        for embeds_file in (data_dir / "courses").glob("**/40-echo360-embeds.md"):
            content = embeds_file.read_text(encoding="utf-8", errors="replace")
            current_title = "Echo360 Video"
            for line in content.splitlines():
                if line.startswith("## "):
                    current_title = line[3:].strip()
                m_uuid = re.search(r"Echo360 UUID:\s*`([a-f0-9\-]+)`", line)
                if m_uuid:
                    uuid_val = m_uuid.group(1)
                    if uuid_val not in seen_embed_uuids:
                        seen_embed_uuids.add(uuid_val)
                        embed_targets.append({
                            "uuid": uuid_val,
                            "title": current_title,
                            "section_id": "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
                            "course_name": "Principles of Business Law (BLAW10001_2026_SM2)",
                            "kb_name": "BLAW10001_2026_SM2",
                        })
    except Exception as exc:
        logger.debug("Failed reading embed targets: %s", exc)

    # Only bootstrap default practice test embeds on initial first run if embeds_file has never been generated
    if not embed_targets and not embeds_file.exists():
        default_uuids = [
            ("acc9fbad-4c0a-4b4d-b8ea-d7727034fe6d", "PBL Practice Test 1(1) - The Nature Of Law"),
            ("9381284d-5253-4e5b-b173-fb47a63576cb", "Workshop 1 - video 2 - law and government in Australia"),
            ("b522c794-bae3-4ca3-a405-ebda6a0c09d7", "PBL Practice Test 1(3) : Case Law - The Courts"),
            ("8387f817-7abd-4bed-8e67-cd49b1b94570", "PBL Practice Test 1(4) : Case Law - Law Reports"),
            ("7692fda3-18d2-40e5-b9c5-2f826b3c35ea", "PBL Practice Test 1(6): Legislation - Making Legislation"),
            ("5a91629c-d690-44d6-ad18-bdd93bd4f4cb", "PBL Practice Test 1(7): Legislation - Citing Legislation"),
            ("107b41dd-1d98-4fb8-8429-bf9801fcf040", "PBL Practice Test 1(8): Legislation: Practical Exercise"),
        ]
        for uid, tname in default_uuids:
            if uid not in seen_embed_uuids:
                seen_embed_uuids.add(uid)
                embed_targets.append({
                    "uuid": uid,
                    "title": tname,
                    "section_id": "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
                    "course_name": "Principles of Business Law (BLAW10001_2026_SM2)",
                    "kb_name": "BLAW10001_2026_SM2",
                })

    # Dynamic LTI launch URL: prefer enrolled Canvas course with numeric ID, defaulting to COMP10001
    lti_launch_url = "https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/701"
    for lib in libs.values():
        cid = str(lib.get("canvas_course_id") or lib.get("id") or "")
        if cid.isdigit() and lib.get("echo360_course_id"):
            lti_launch_url = f"https://canvas.lms.unimelb.edu.au/courses/{cid}/external_tools/701"
            break

    return {
        "targets": targets,
        "embed_targets": embed_targets,
        "ingested_echo360_ids": existing_ids,
        "lti_launch_url": lti_launch_url,
        "echo360_content_url": "https://echo360.net.au/content",
    }


@router.get("/libraries/{library_id}")
async def get_course_library(library_id: str) -> dict:
    """Get details for a specific Course Library."""
    libs = _load_libraries()
    if library_id not in libs:
        raise HTTPException(404, "Course library not found.")
    return libs[library_id]


@router.put("/libraries/{library_id}")
async def update_course_library(library_id: str, payload: CourseLibraryUpdate) -> dict:
    """Update settings for a Course Library."""
    libs = _load_libraries()
    if library_id not in libs:
        raise HTTPException(404, "Course library not found.")
    lib = libs[library_id]
    if payload.name is not None:
        lib["name"] = payload.name
    if payload.kb_name is not None:
        lib["kb_name"] = payload.kb_name
        _ensure_kb_exists(payload.kb_name)
    if payload.echo360_course_id is not None:
        lib["echo360_course_id"] = payload.echo360_course_id
    if payload.canvas_course_id is not None:
        lib["canvas_course_id"] = payload.canvas_course_id
    if payload.image_download_url is not None:
        lib["image_download_url"] = payload.image_download_url
    if payload.auto_sync is not None:
        lib["auto_sync"] = payload.auto_sync
    libs[library_id] = lib
    _save_libraries(libs)
    return lib


@router.delete("/libraries/{library_id}")
async def delete_course_library(library_id: str) -> dict:
    """Delete a Course Library registration."""
    libs = _load_libraries()
    if library_id not in libs:
        raise HTTPException(404, "Course library not found.")
    removed = libs.pop(library_id)
    _save_libraries(libs)
    return {"deleted": True, "library": removed}


@router.delete("/{item_id}")
async def delete_lecture_item(item_id: str) -> dict:
    """Delete a media library item directory by ID."""
    item_dir = _root() / item_id
    if not item_dir.exists() or not item_dir.is_dir():
        raise HTTPException(404, "Media item not found.")
    shutil.rmtree(item_dir, ignore_errors=True)
    _invalidate_list_cache()
    return {"deleted": True, "id": item_id}


# ── Lecture Thumbnail Ingestion ─────────────────────────────────────────────
class ThumbnailIngestItem(BaseModel):
    lesson_id: str = Field(min_length=1, max_length=256)
    media_id: str | None = Field(default=None, max_length=256)
    image_b64: str = Field(min_length=1)


class ThumbnailIngestRequest(BaseModel):
    thumbnails: list[ThumbnailIngestItem]


@router.post("/thumbnails/ingest")
async def ingest_lecture_thumbnails(payload: ThumbnailIngestRequest) -> dict[str, Any]:
    """Store base64 lecture thumbnails harvested from Echo360 into cache."""
    cache_dir = get_path_service().get_user_root() / "cache" / "echo360_thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in payload.thumbnails:
        b64_str = item.image_b64
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(b64_str)
            if len(raw_bytes) > 50:
                safe_lid = re.sub(r"[^a-zA-Z0-9_\-]", "_", item.lesson_id)
                (cache_dir / f"{safe_lid}.jpg").write_bytes(raw_bytes)
                if item.media_id:
                    safe_mid = re.sub(r"[^a-zA-Z0-9_\-]", "_", item.media_id)
                    (cache_dir / f"{safe_mid}.jpg").write_bytes(raw_bytes)
                saved += 1
        except Exception as e:
            logger.debug("Failed saving thumbnail for %s: %s", item.lesson_id, e)
    return {"status": "ok", "saved_count": saved}


# ── Lecture Subtitle Ingestion ──────────────────────────────────────────────
class SubtitleIngestItem(BaseModel):
    lesson_id: str = Field(min_length=1, max_length=256)
    vtt: str = Field(min_length=1, max_length=2000000)


class SubtitleIngestRequest(BaseModel):
    subtitles: list[SubtitleIngestItem]


@router.post("/subtitles/ingest")
async def ingest_lecture_subtitles(payload: SubtitleIngestRequest) -> dict[str, Any]:
    """Store VTT subtitle tracks harvested from Echo360 pages into cache."""
    cache_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in payload.subtitles:
        text = (item.vtt or "").strip()
        if not text.startswith("WEBVTT"):
            continue
        try:
            safe_lid = re.sub(r"[^a-zA-Z0-9_\-]", "_", item.lesson_id)
            (cache_dir / f"{safe_lid}.vtt").write_text(text, encoding="utf-8")
            saved += 1
        except Exception as e:
            logger.debug("Failed saving subtitles for %s: %s", item.lesson_id, e)
    return {"status": "ok", "saved_count": saved}


@router.post("/media/ingest")
async def ingest_lecture_media(
    request: Request,
    background_tasks: BackgroundTasks,
    lesson_id: str = Query(..., min_length=1),
    title: str = Query("Echo360 Video", min_length=1),
    course_name: str = Query("Principles of Business Law (BLAW10001_2026_SM2)"),
    course_id: str = Query("4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a"),
    kb_name: str = Query("BLAW10001_2026_SM2"),
) -> dict[str, Any]:
    """Ingest media file uploaded directly from authenticated browser bridge."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty media payload")

    item_dir = _find_echo360_item_dir(lesson_id)
    if item_dir:
        item_id = item_dir.name
    else:
        _cleanup_failed_echo360_id(lesson_id)
        item_id = uuid4().hex
        item_dir = _root() / item_id
        item_dir.mkdir(parents=True, exist_ok=True)

    media_file_path = item_dir / "media.mp4"
    media_file_path.write_bytes(data)

    lecture_date = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": item_id,
        "title": title,
        "original_name": f"{title}.mp4",
        "content_type": "video/mp4",
        "media_file": "media.mp4",
        "kb_name": kb_name,
        "status": "downloaded",
        "progress": "Archived instantly from Echo360 via browser bridge.",
        "created_at": lecture_date,
        "source": "echo360",
        "source_id": lesson_id,
        "source_url": f"https://echo360.net.au/lesson/{lesson_id}/classroom",
        "institution": "University of Melbourne",
        "course_id": course_id,
        "course_name": course_name,
        "series_name": course_name,
        "lecture_date": lecture_date,
    }
    _write(item_dir, meta)

    try:
        from unisync.core.download_registry import get_registry
        get_registry().register(
            platform="echo360",
            item_id=str(lesson_id),
            title=title,
            direct_link=f"https://echo360.net.au/lesson/{lesson_id}/classroom",
            local_filepath=media_file_path,
            course_id=course_id,
            course_name=course_name,
            size_bytes=len(data),
            content_type="video/mp4",
            metadata={
                "source_url": meta.get("source_url", ""),
                "lecture_date": lecture_date,
                "kb_name": kb_name,
            },
        )
    except Exception as reg_err:
        logger.debug("Failed registering in download registry: %s", reg_err)

    try:
        from unisync.core.path_service import get_path_service
        from unisync.core.file_io import atomic_write_json as _atomic_write_json
        embed_path = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
        if embed_path.exists():
            embed_data = json.loads(embed_path.read_text(encoding="utf-8"))
            embed_key = lesson_id.removeprefix("BLAW_EMBED_")
            embed_uuid_part = embed_key.split("_", 1)[0] if "_" in embed_key else embed_key
            embed_link_part = embed_key.split("_", 1)[1] if "_" in embed_key else ""
            changed = False
            for _cid, _items in embed_data.items():
                if _cid != "240718" and _cid != course_id:
                    continue
                for embed in _items or []:
                    _cat_uuid = str(embed.get("uuid") or embed.get("media_id") or "")
                    _cat_media = str(embed.get("media_id") or "")
                    _cat_link = str(embed.get("link_id") or "")
                    if embed_link_part and _cat_link:
                        is_match = _cat_link.startswith(embed_link_part)
                    else:
                        is_match = embed_uuid_part in (_cat_uuid, _cat_media) or embed_key in (_cat_uuid, _cat_media)
                    if is_match:
                        embed["downloaded"] = True
                        embed["downloaded_at"] = lecture_date
                        changed = True
            if changed:
                _atomic_write_json(embed_path, embed_data)
                logger.info("Marked %s as downloaded in echo360_embeds.json", lesson_id)
    except Exception as em_err:
        logger.debug("Failed updating download state in catalog: %s", em_err)

    background_tasks.add_task(_process, item_id)
    logger.info("Archived Echo360 media for %s (%s, %d bytes)", lesson_id, title, len(data))
    return {"status": "ok", "item_id": item_id, "bytes": len(data)}


@router.post("/echo360/import")
async def import_echo360_course(
    payload: Echo360ImportRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Queue browser-resolved sources into the normal media-library pipeline."""
    # Resolve target kb_name from registered course libraries if omitted
    if not payload.kb_name and payload.course_id:
        libs = _load_libraries()
        for lib in libs.values():
            if lib.get("echo360_course_id") == payload.course_id:
                payload.kb_name = lib.get("kb_name", "")
                break

    if payload.kb_name:
        _ensure_kb_exists(payload.kb_name)

    existing = _existing_echo360_ids(include_failed=False)
    queued: list[dict[str, Any]] = []
    skipped = 0
    for source in payload.sources:
        if source.course_id != payload.course_id:
            raise HTTPException(422, "Every selected recording must belong to the selected course.")
        _validated_echo360_media_url(source.media_url)
        if source.id in existing:
            skipped += 1
            continue
        _cleanup_failed_echo360_id(source.id)
        item_id = uuid4().hex
        item_dir = _root() / item_id
        item_dir.mkdir(parents=True)
        original_name = _safe_media_title(source)
        meta = {
            "id": item_id,
            "title": source.title,
            "original_name": original_name,
            "content_type": "video/mp4",
            "media_file": "media.mp4",
            "kb_name": payload.kb_name,
            "status": "queued",
            "progress": "Queued from UniMelb Echo360",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "echo360",
            "source_id": source.id,
            # The resolved URL can be short-lived; do not persist it in metadata.
            "source_url": f"https://echo360.net.au/lesson/{source.id}/classroom",
            "institution": "University of Melbourne",
            "course_id": payload.course_id,
            "course_name": source.course_name,
            "lecture_date": source.date,
        }
        _write(item_dir, meta)
        background_tasks.add_task(
            _download_and_process_echo360,
            item_id,
            source,
        )
        queued.append(meta)
        existing.add(source.id)
    return {"queued": queued, "skipped": skipped}



@router.get("/{item_id}")
async def get_library_item(item_id: str) -> dict:
    return _read(_root() / item_id)


@router.get("/{item_id}/media")
async def stream_library_media(item_id: str) -> FileResponse:
    item_dir = _root() / item_id
    if not item_dir.exists() or not (item_dir / "metadata.json").exists():
        # Fallback: check if item_id matches source_id, id, or media_id in any media directory
        for p in _root().iterdir():
            if p.is_dir() and (p / "metadata.json").exists():
                try:
                    candidate = _read(p)
                    if (
                        candidate.get("source_id") == item_id
                        or candidate.get("id") == item_id
                        or candidate.get("media_id") == item_id
                    ):
                        item_dir = p
                        break
                except Exception:
                    continue
    if not item_dir.exists() or not (item_dir / "metadata.json").exists():
        raise HTTPException(404, f"Lecture media item not found: {item_id}")

    meta = _read(item_dir)
    media_file = meta.get("media_file")
    if not media_file or not (item_dir / media_file).exists():
        # Plex: return clear message when media not yet downloaded so
        # the frontend can show a "downloading" placeholder instead of a
        # broken player.  Starlette's FileResponse already handles Range
        # requests for seeking once the file is present.
        raise HTTPException(
            404,
            f"Media not yet downloaded — status: {meta.get('status', 'unknown')}",
        )
    return FileResponse(
        item_dir / media_file,
        media_type=meta.get("content_type", "video/mp4"),
        filename=meta.get("original_name", "media.mp4"),
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/{item_id}/subtitles")
async def get_library_subtitles(item_id: str):
    # 1. Check cached subtitles in data/cache/echo360_subtitles
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", item_id)
    uuids = re.findall(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", item_id, re.I)
    cache_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
    vtt_candidates = [
        cache_dir / f"{safe_id}.vtt",
        cache_dir / f"{item_id.replace(':', '_')}.vtt",
        cache_dir / f"{item_id}.vtt",
    ]
    for u in uuids:
        vtt_candidates.append(cache_dir / f"{u}.vtt")
        vtt_candidates.append(cache_dir / f"BLAW_EMBED_{u}.vtt")
    for vc in vtt_candidates:
        if vc.exists() and vc.stat().st_size > 0:
            return PlainTextResponse(
                vc.read_text(encoding="utf-8"),
                media_type="text/vtt; charset=utf-8",
            )
    if cache_dir.exists():
        for vc in cache_dir.glob("*.vtt"):
            if safe_id in vc.stem or vc.stem in safe_id:
                return PlainTextResponse(
                    vc.read_text(encoding="utf-8"),
                    media_type="text/vtt; charset=utf-8",
                )

    # 2. Check local media_library item
    item_dir = _root() / item_id
    if item_dir.exists():
        meta = _read(item_dir)
        filename = meta.get("subtitle_file")
        if filename and (item_dir / filename).exists():
            return PlainTextResponse(
                (item_dir / filename).read_text(encoding="utf-8"),
                media_type="text/vtt; charset=utf-8",
            )

    # 3. Check syllabus cache
    syl_path = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
    if syl_path.exists():
        try:
            syl_data = json.loads(syl_path.read_text("utf-8"))
            for sec in syl_data:
                for syl_item in sec.get("syllabus", []):
                    les = syl_item.get("lesson", {})
                    l_obj = les.get("lesson", les)
                    if l_obj.get("id") == item_id:
                        if syl_item.get("subtitles"):
                            return PlainTextResponse(
                                syl_item["subtitles"],
                                media_type="text/vtt; charset=utf-8",
                            )
        except Exception:
            pass

    raise HTTPException(404, "Subtitles are not ready.")


# ── Plex fix: GET /{item_id}/words ────────────────────────────────────────────
# The frontend's `loadWordTimings` effect hits this endpoint first.  It returns
# word-level segment timing so the karaoke overlay and transcript sidepane can
# highlight individual words in sync with playback.  Falls back to 404 when no
# VTT file has been generated yet (processing still in progress).
@router.get("/{item_id}/words")
async def get_library_words(item_id: str):
    """Return parsed word-level segment timings for a media item.

    Reads the stored ``subtitles.vtt`` file and parses it with ``_vtt_to_segments``
    (mirrors the frontend ``parseVttToSegments``).  Returns
    ``{"segments": [...]}`` on success, or 404 if no VTT is available.
    """
    item_dir = _root() / item_id
    meta = _read(item_dir)
    filename = meta.get("subtitle_file")
    if not filename:
        raise HTTPException(404, "Subtitles not generated yet.")
    vtt_path = item_dir / filename
    if not vtt_path.exists():
        raise HTTPException(404, "Subtitle file missing on disk.")
    vtt_text = vtt_path.read_text(encoding="utf-8")
    segments = _vtt_to_segments(vtt_text)
    if not segments:
        raise HTTPException(404, "VTT file contained no parseable segments.")
    return {"segments": segments}


# ── Scrubbing Thumbnails: GET /{item_id}/thumbnails.vtt & /{item_id}/thumbnail ─
@router.get("/{item_id}/thumbnails.vtt")
async def get_library_thumbnails_vtt(item_id: str):
    """Return a WebVTT storyboard descriptor for video scrubber preview."""
    item_dir = _root() / item_id
    meta = _read(item_dir)
    duration = float(meta.get("duration_seconds") or 3600)
    interval = 5.0  # 1 thumbnail frame every 5 seconds
    lines = ["WEBVTT", ""]
    curr = 0.0
    while curr < duration:
        nxt = min(curr + interval, duration)
        t1 = f"{int(curr//3600):02d}:{int((curr%3600)//60):02d}:{curr%60:06.3f}"
        t2 = f"{int(nxt//3600):02d}:{int((nxt%3600)//60):02d}:{nxt%60:06.3f}"
        lines.append(f"{t1} --> {t2}")
        lines.append(f"/api/v1/lectures/{item_id}/thumbnail?time={curr + 0.5}")
        lines.append("")
        curr += interval
    return PlainTextResponse("\n".join(lines), media_type="text/vtt; charset=utf-8")


def _generate_thumbnail_svg(title: str = "UniMelb Lecture", course_code: str = "") -> str:
    """Generate a clean, high-contrast, course-branded SVG video poster for lectures without a downloaded image."""
    clean_course = (course_code or "UNIMELB").split("_")[0].upper()

    palettes = {
        "COMP10001": {
            "name": "Foundations of Computing",
            "dept": "School of Computing & Information Systems",
            "accent": "#38bdf8",
            "glow": "#0284c7",
            "bg_start": "#0c1a2e",
            "bg_end": "#030712",
            "border": "rgba(56, 189, 248, 0.25)",
        },
        "BLAW10001": {
            "name": "Principles of Business Law",
            "dept": "Melbourne Law School & Business",
            "accent": "#fbbf24",
            "glow": "#d97706",
            "bg_start": "#221708",
            "bg_end": "#080603",
            "border": "rgba(251, 191, 36, 0.25)",
        },
        "INFO10003": {
            "name": "Fundamentals of Interaction Design",
            "dept": "Interaction Design & Digital Systems",
            "accent": "#c084fc",
            "glow": "#7c3aed",
            "bg_start": "#1e0f33",
            "bg_end": "#07030d",
            "border": "rgba(192, 132, 252, 0.25)",
        },
        "CCDP10003": {
            "name": "Video Games: Remaking Reality",
            "dept": "Faculty of Arts • Media & Communications",
            "accent": "#34d399",
            "glow": "#059669",
            "bg_start": "#0b2419",
            "bg_end": "#020906",
            "border": "rgba(52, 211, 153, 0.25)",
        },
    }

    pal = palettes.get(clean_course, {
        "name": "University Course",
        "dept": "University of Melbourne",
        "accent": "#60a5fa",
        "glow": "#2563eb",
        "bg_start": "#111827",
        "bg_end": "#030712",
        "border": "rgba(96, 165, 250, 0.25)",
    })

    raw_title = (title or "Lecture Video").strip()
    raw_title = re.sub(r"\s*\(commentary\)\s*2026|\s*2026|\.mp4", "", raw_title, flags=re.I).strip()
    safe_title = raw_title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    words = safe_title.split()
    line1 = ""
    line2 = ""
    if len(safe_title) <= 34:
        line1 = safe_title
    else:
        l1_words: list[str] = []
        l2_words: list[str] = []
        for w in words:
            if len(" ".join(l1_words + [w])) <= 32:
                l1_words.append(w)
            else:
                l2_words.append(w)
        line1 = " ".join(l1_words)
        line2 = " ".join(l2_words)
        if len(line2) > 34:
            line2 = line2[:32] + "..."

    badge_w = max(80, len(clean_course) * 10 + 24)

    if line2:
        title_markup = f"""<text x="40" y="254" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="20" font-weight="700" letter-spacing="-0.02em">{line1}</text>
  <text x="40" y="282" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="20" font-weight="700" letter-spacing="-0.02em">{line2}</text>"""
    else:
        title_markup = f"""<text x="40" y="278" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="22" font-weight="700" letter-spacing="-0.02em">{line1}</text>"""

    safe_dept = (pal.get("dept") or "University of Melbourne").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_course = clean_course.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360" fill="none">
  <defs>
    <linearGradient id="bg-grad" x1="0" y1="0" x2="640" y2="360" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="{pal['bg_start']}"/>
      <stop offset="100%" stop-color="{pal['bg_end']}"/>
    </linearGradient>
    <radialGradient id="glow-grad" cx="80%" cy="20%" r="60%">
      <stop offset="0%" stop-color="{pal['glow']}" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="{pal['glow']}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="bottom-glow" cx="30%" cy="90%" r="50%">
      <stop offset="0%" stop-color="{pal['glow']}" stop-opacity="0.14"/>
      <stop offset="100%" stop-color="{pal['glow']}" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- Background Base -->
  <rect width="640" height="360" fill="#05070a"/>
  <rect width="640" height="360" fill="url(#bg-grad)"/>
  <rect width="640" height="360" fill="url(#glow-grad)"/>
  <rect width="640" height="360" fill="url(#bottom-glow)"/>

  <!-- Subtle Outer Framing -->
  <rect x="16" y="16" width="608" height="328" rx="14" stroke="rgba(255,255,255,0.06)" stroke-width="1.5"/>

  <!-- Decorative Graphic Lines -->
  <circle cx="560" cy="80" r="140" stroke="{pal['border']}" stroke-width="1" stroke-dasharray="4 6" opacity="0.4"/>
  <circle cx="560" cy="80" r="90" stroke="{pal['border']}" stroke-width="1" opacity="0.25"/>

  <!-- Central Play Glassmorphism Disc -->
  <g transform="translate(288, 110)">
    <circle cx="32" cy="32" r="32" fill="#0f172a" fill-opacity="0.75" stroke="{pal['border']}" stroke-width="1.5"/>
    <polygon points="27,20 45,32 27,44" fill="{pal['accent']}"/>
  </g>

  <!-- Top Subject Badge -->
  <rect x="40" y="38" width="{badge_w}" height="26" rx="6" fill="#0f172a" fill-opacity="0.85" stroke="{pal['border']}" stroke-width="1"/>
  <text x="52" y="55" fill="{pal['accent']}" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="11" font-weight="700" letter-spacing="0.08em">{safe_course}</text>

  <!-- Top Right Watermark -->
  <text x="595" y="55" fill="rgba(255,255,255,0.2)" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="10" font-weight="600" letter-spacing="0.1em" text-anchor="end">UNIMELB LECTURE</text>

  <!-- Title Section -->
  {title_markup}

  <!-- Footer Metadata -->
  <text x="40" y="312" fill="#64748b" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="11" font-weight="500">{safe_dept}</text>
</svg>"""


@router.get("/{item_id}/thumbnail")
async def get_library_thumbnail(item_id: str, time: float = 0.0):
    """Return a JPEG thumbnail frame or cached Echo360 poster, with fallback SVG."""
    cache_dir = get_path_service().get_user_root() / "cache" / "echo360_thumbnails"
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", item_id)
    uuids = re.findall(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", item_id, re.I)

    # 1. Direct match in cache_dir
    candidate_paths = [
        cache_dir / f"{safe_id}.jpg",
        cache_dir / f"{item_id}.jpg",
    ]
    for u in uuids:
        candidate_paths.append(cache_dir / f"{u}.jpg")
        candidate_paths.append(cache_dir / f"BLAW_EMBED_{u}.jpg")

    # Match media_id / siblings from echo360_embeds.json for embedded lectures
    embeds_path = get_path_service().get_user_root() / "libraries" / "echo360_embeds.json"
    matched_emb = None
    if embeds_path.exists():
        try:
            emb_data = json.loads(embeds_path.read_text("utf-8"))
            target_link_prefix = None
            parts = item_id.split("_")
            if len(parts) >= 4 and len(parts[-1]) == 8:
                target_link_prefix = parts[-1].lower()

            for cid, items in emb_data.items():
                if matched_emb:
                    break
                for it in items or []:
                    u = (it.get("uuid") or it.get("media_id") or "").lower()
                    link = (it.get("link_id") or "").lower()
                    if target_link_prefix and link.startswith(target_link_prefix):
                        matched_emb = it
                        break
                    lid1 = f"BLAW_EMBED_{u}_{link[:8]}" if link else f"BLAW_EMBED_{u}"
                    if lid1.lower() == item_id.lower() or f"BLAW_EMBED_{u}".lower() == item_id.lower():
                        matched_emb = it
                        break

            if matched_emb:
                emb_title = matched_emb.get("title") or matched_emb.get("source_title") or ""
                for val in [matched_emb.get("media_id"), matched_emb.get("uuid")]:
                    if val:
                        candidate_paths.append(cache_dir / f"{val}.jpg")
                        candidate_paths.append(cache_dir / f"BLAW_EMBED_{val}.jpg")
                for mu in re.findall(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", matched_emb.get("media_url") or "", re.I):
                    candidate_paths.append(cache_dir / f"{mu}.jpg")
                    candidate_paths.append(cache_dir / f"BLAW_EMBED_{mu}.jpg")

                # Sibling match: find any other embed with the same title or module
                clean_target_title = _resolve_clean_title(emb_title, matched_emb.get("source_title"), matched_emb.get("uuid"), matched_emb.get("link_id"), matched_emb.get("media_url"))
                target_mod = re.search(r"\b([1-9]\.[0-9]+)\b", clean_target_title)
                target_mod_str = target_mod.group(1) if target_mod else None

                for cid, items in emb_data.items():
                    for it in items or []:
                        st = it.get("title") or it.get("source_title") or ""
                        cl_st = _resolve_clean_title(st, it.get("source_title"), it.get("uuid"), it.get("link_id"), it.get("media_url"))
                        st_mod = re.search(r"\b([1-9]\.[0-9]+)\b", cl_st)
                        st_mod_str = st_mod.group(1) if st_mod else None

                        is_title_match = (
                            cl_st.lower().strip() == clean_target_title.lower().strip()
                            or (emb_title and emb_title.lower().replace(".mp4", "").strip() == st.lower().replace(".mp4", "").strip())
                        )
                        is_mod_match = bool(target_mod_str and st_mod_str and target_mod_str == st_mod_str)

                        if is_title_match or is_mod_match:
                            for s_val in [it.get("media_id"), it.get("uuid")]:
                                if s_val:
                                    candidate_paths.append(cache_dir / f"{s_val}.jpg")
                                    candidate_paths.append(cache_dir / f"BLAW_EMBED_{s_val}.jpg")
                            for smu in re.findall(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", it.get("media_url") or "", re.I):
                                candidate_paths.append(cache_dir / f"{smu}.jpg")
                                candidate_paths.append(cache_dir / f"BLAW_EMBED_{smu}.jpg")
        except Exception:
            pass

    # Match media_id from syllabus if item_id is a lesson ID
    syl_file = get_path_service().get_user_root() / "cache" / "echo360_syllabus.json"
    if syl_file.exists():
        try:
            syl_data = json.loads(syl_file.read_text("utf-8"))
            for sec in syl_data:
                for syl_item in sec.get("syllabus", []):
                    les = syl_item.get("lesson", {})
                    l_obj = les.get("lesson", les)
                    if l_obj.get("id") == item_id or any(m.get("id") == item_id for m in les.get("medias", [])):
                        for m in les.get("medias", []):
                            mid = m.get("id")
                            if mid:
                                candidate_paths.append(cache_dir / f"{mid}.jpg")
                                candidate_paths.append(cache_dir / f"BLAW_EMBED_{mid}.jpg")
                        break
        except Exception:
            pass

    for cp in candidate_paths:
        if cp.exists() and cp.stat().st_size > 0:
            return FileResponse(
                cp,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )

    # 2. Fuzzy match in cache_dir
    if cache_dir.exists():
        for cp in cache_dir.glob("*.jpg"):
            if safe_id in cp.stem or cp.stem in safe_id:
                if cp.stat().st_size > 0:
                    return FileResponse(
                        cp,
                        media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )

    # 3. Media item on disk (for downloaded items)
    item_dir = _root() / item_id
    if item_dir.exists():
        thumb_dir = item_dir / "thumbnails"
        bucket = int(round(max(0.0, time) / 5.0) * 5)
        thumb_path = thumb_dir / f"thumb_{bucket}.jpg"
        if thumb_path.exists():
            return FileResponse(
                thumb_path,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        meta = _read(item_dir)
        media_file = meta.get("media_file")
        if media_file:
            media_path = item_dir / media_file
            if media_path.exists():
                ffmpeg_bin = shutil.which("ffmpeg")
                if ffmpeg_bin:
                    try:
                        thumb_dir.mkdir(exist_ok=True)
                        cmd = [
                            ffmpeg_bin,
                            "-ss", str(bucket),
                            "-i", str(media_path),
                            "-frames:v", "1",
                            "-q:v", "4",
                            "-vf", "scale=480:-1",
                            "-y",
                            str(thumb_path),
                        ]
                        res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
                        if res.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0:
                            return FileResponse(
                                thumb_path,
                                media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"},
                            )
                    except Exception:
                        pass

    # 4. Fallback: Lookup title and course in syllabus or metadata for SVG poster
    title = "UniMelb Lecture"
    course_code = "BLAW10001" if item_id.startswith("BLAW_") else ""
    if item_dir.exists():
        meta = _read(item_dir)
        title = meta.get("title") or meta.get("original_name") or title
        course_code = meta.get("kb_name") or meta.get("course_name") or course_code
    else:
        for u in uuids:
            if u in _KNOWN_STANDALONE_TITLES:
                title = _KNOWN_STANDALONE_TITLES[u]
                course_code = "BLAW10001"
                break
            if u in _PRACTICE_TEST_1_TITLES:
                title = _PRACTICE_TEST_1_TITLES[u]
                course_code = "BLAW10001"
                break
        if title == "UniMelb Lecture" and matched_emb:
            title = _resolve_clean_title(
                matched_emb.get("title"),
                matched_emb.get("source_title"),
                matched_emb.get("uuid"),
                matched_emb.get("link_id"),
                matched_emb.get("media_url"),
                matched_emb.get("player_url") or matched_emb.get("canvas_launch_url"),
            )
            course_code = "BLAW10001"

        if (title == "UniMelb Lecture" or not course_code) and syl_file.exists():
            try:
                syl_data = json.loads(syl_file.read_text("utf-8"))
                libs = _load_libraries()
                sec_map = {lib.get("echo360_course_id"): lib for lib in libs.values() if lib.get("echo360_course_id")}
                for sec in syl_data:
                    sec_id = sec.get("sectionId")
                    lib_info = sec_map.get(sec_id, {})
                    for syl_item in sec.get("syllabus", []):
                        les = syl_item.get("lesson", {})
                        l_obj = les.get("lesson", les)
                        if l_obj.get("id") == item_id or any(m.get("id") == item_id for m in les.get("medias", [])):
                            raw_name = l_obj.get("name") or l_obj.get("displayName") or title
                            timing = l_obj.get("timing") or {}
                            start = timing.get("start", "")
                            title = _clean_timetable_title(raw_name, start)
                            course_code = lib_info.get("kb_name", "").split("_")[0] or lib_info.get("name", "").split("(")[0].strip() or course_code
                            break
            except Exception:
                pass

    svg_content = _generate_thumbnail_svg(title=title, course_code=course_code)
    return Response(
        content=svg_content,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/{item_id}/{artifact}")
async def get_library_artifact(item_id: str, artifact: str) -> PlainTextResponse:
    if artifact not in {"transcript", "notes"}:
        raise HTTPException(404, "Artifact not found.")
    item_dir = _root() / item_id
    meta = _read(item_dir)
    filename = meta.get(f"{artifact}_file")
    if not filename:
        raise HTTPException(404, f"{artifact.title()} is not ready.")
    return PlainTextResponse((item_dir / filename).read_text(encoding="utf-8"))




@router.post("/{item_id}/reprocess")
async def reprocess_library_item(item_id: str, background_tasks: BackgroundTasks) -> dict:
    item_dir = _root() / item_id
    if not item_dir.exists():
        raise HTTPException(404, "Lecture item not found.")
    meta = _read(item_dir)
    background_tasks.add_task(_process, item_id)
    return {"status": "queued", "message": "Reprocessing started in background."}


# ---------------------------------------------------------------------------
# Plex: retroactive title fix — re-title old items with bad titles
# ---------------------------------------------------------------------------
# Why: Old items (08-15 range) have copyright disclaimers or Whisper tag
# contamination as their title.  New items (08-25) have "<v Speaker 0>"
# prefixes.  This one-time migration reads transcript.txt, calls the LLM
# to produce a clean title + description, and updates metadata.json.
# ---------------------------------------------------------------------------

_TITLE_BAD_PATTERNS: tuple[str, ...] = (
    "This material is made available",
    "<v Speaker",
    "<vspeaker",
)


def _title_looks_bad(title: str) -> bool:
    """Return True if *title* is likely a copyright disclaimer or Whisper tag."""
    t = title.lower()
    for pat in _TITLE_BAD_PATTERNS:
        if pat.lower() in t:
            return True
    # Catch titles that are just a spoken sentence fragment (no real topic)
    if len(title) > 80:
        return True
    return False


async def _retitle_existing(item_id: str) -> dict[str, str]:
    """Re-title a single media library item using its on-disk transcript.

    Returns ``{"old": ..., "new": ...}`` or ``{"skipped": "..."}``.
    """
    item_dir = _root() / item_id
    if not item_dir.exists():
        return {"skipped": f"{item_id}: directory missing"}
    meta = _read(item_dir)
    if meta.get("status") != "ready":
        return {"skipped": f"{item_id}: status={meta.get('status')}"}
    title = meta.get("title", "")
    if not _title_looks_bad(title):
        return {"skipped": f"{item_id}: title already ok"}
    transcript_path = item_dir / "transcript.txt"
    if not transcript_path.exists():
        return {"skipped": f"{item_id}: no transcript on disk"}
    transcript = transcript_path.read_text(encoding="utf-8")
    if not transcript.strip():
        return {"skipped": f"{item_id}: transcript empty"}
    try:
        new_title, new_desc = await _generate_title_and_description(
            transcript, language=meta.get("language", "en"),
        )
    except Exception as exc:
        return {"skipped": f"{item_id}: LLM error: {exc}"}
    if not new_title:
        return {"skipped": f"{item_id}: LLM returned empty title"}
    meta["title"] = new_title
    meta["quick_description"] = new_desc
    meta["description"] = new_desc
    _write(item_dir, meta)
    return {"old": title, "new": new_title}


@router.post("/retitle-all")
async def retitle_all() -> dict:
    """Scan all ready media items and re-title those with bad titles.

    Runs inline with a lightweight semaphore (max 2 concurrent LLM calls)
    to avoid flooding the provider.  For very large libraries the caller
    may prefer a single-item POST /{item_id}/retitle instead.
    """
    root_dir = _root()
    if not root_dir.exists():
        return {"retitled": [], "skipped": 0}

    # Collect candidate item IDs (only "ready" items with bad titles)
    candidates: list[str] = []
    for item_dir in root_dir.iterdir():
        if not item_dir.is_dir() or not (item_dir / "metadata.json").exists():
            continue
        try:
            meta = _read(item_dir)
        except HTTPException:
            continue
        if meta.get("status") != "ready":
            continue
        title = meta.get("title", "")
        if _title_looks_bad(title) and (item_dir / "transcript.txt").exists():
            candidates.append(item_dir.name)

    if not candidates:
        return {"retitled": [], "skipped": 0}

    # Plex: retroactive title fix — process with concurrency cap
    sem = asyncio.Semaphore(2)
    retitled: list[dict] = []
    skipped = 0

    async def _guarded_retitle(cid: str) -> None:
        nonlocal skipped
        async with sem:
            result = await _retitle_existing(cid)
            if "skipped" in result:
                skipped += 1
            else:
                retitled.append({"id": cid, **result})

    await asyncio.gather(*[_guarded_retitle(cid) for cid in candidates])
    return {"retitled": retitled, "skipped": skipped}
