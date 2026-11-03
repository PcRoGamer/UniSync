"""UniSync On-Disk Academic Material Sanitizer Service.

Provides automatic and background scrubbing of local markdown and text course
materials across data/courses and data/cache, stripping institutional disclaimers,
Turnitin policies, and GenAI declaration headers in-place.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from unisync.core.text_cleaner import filter_academic_boilerplate, filter_vtt_content

logger = logging.getLogger("unisync.disk_cleaner")

_clean_lock = threading.Lock()

QUICK_KEYWORDS: tuple[str, ...] = (
    "academic integrity",
    "academic honesty",
    "misconduct",
    "turnitin",
    "plagiar",
    "collusion",
    "chatgpt",
    "generative ai",
    "artificial intelligence",
    "genai",
    "ai declaration",
    "ai use",
    "ai policy",
    "ai tool",
    "use of ai",
    "no ai",
    "feit ai",
    "integrity check",
    "integrity rule",
    "integrity require",
    "al-generator",
    "run and check",
    "run(green)",
    "check(blue)",
)


def _get_default_target_paths() -> list[Path]:
    """Resolves data paths to scrub based on PathService or repository root."""
    paths: list[Path] = []
    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
        courses_dir = user_root / "courses"
        cache_dir = user_root / "cache"
        if courses_dir.exists():
            paths.append(courses_dir)
        if cache_dir.exists():
            paths.append(cache_dir)
    except Exception:
        pass

    if not paths:
        # Fallback to repo root data/
        repo_root = Path(__file__).resolve().parents[3]
        data_dir = repo_root / "data"
        if (data_dir / "courses").exists():
            paths.append(data_dir / "courses")
        if (data_dir / "cache").exists():
            paths.append(data_dir / "cache")
        if not paths and data_dir.exists():
            paths.append(data_dir)

    return paths


def clean_disk(
    target_path: Path | str | None = None,
    verbose: bool = False,
) -> tuple[int, int]:
    """Scans and sanitizes files in-place across the target directory.

    Thread-safe; if another clean pass is currently running, waits for it.
    Returns (scanned_count, modified_count).
    """
    with _clean_lock:
        if target_path:
            targets = [Path(target_path)]
        else:
            targets = _get_default_target_paths()

        scanned = 0
        modified = 0

        for base in targets:
            if not base.exists():
                continue

            for p in base.glob("**/*"):
                ext = p.suffix.lower()
                if not p.is_file() or ext not in (".md", ".txt", ".vtt"):
                    continue
                if p.name.startswith("."):
                    continue

                scanned += 1
                try:
                    if ext == ".vtt":
                        raw = p.read_text(encoding="utf-8", errors="replace")
                        cleaned = filter_vtt_content(raw)
                        if cleaned != raw:
                            p.write_text(cleaned, encoding="utf-8")
                            modified += 1
                            if verbose:
                                logger.info("Sanitized VTT transcript in-place: %s", p.name)
                        continue

                    raw = p.read_text(encoding="utf-8", errors="replace")
                    raw_lower = raw.lower()
                    if any(kw in raw_lower for kw in QUICK_KEYWORDS):
                        cleaned = filter_academic_boilerplate(raw)
                        if cleaned != raw:
                            p.write_text(cleaned, encoding="utf-8")
                            modified += 1
                            if verbose:
                                logger.info("Sanitized in-place: %s", p)
                except Exception as exc:
                    logger.debug("Error scrubbing file %s: %s", p.name, exc)

        if modified > 0 or verbose:
            logger.info(
                "On-disk sanitizer complete: %d files scanned, %d sanitized in-place.",
                scanned,
                modified,
            )
        return scanned, modified


def run_background_disk_scrubber(
    target_path: Path | str | None = None,
    delay_sec: float = 0.0,
    verbose: bool = False,
) -> threading.Thread:
    """Launches the on-disk scrubber in a background daemon thread.

    Non-blocking, zero impact on caller latency or MCP stdio JSON-RPC.
    """
    def _worker():
        if delay_sec > 0:
            time.sleep(delay_sec)
        # Attempt to acquire lock non-blocking; if already running, skip this redundant cycle
        acquired = _clean_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("Disk scrubber is already actively running in another thread; skipping cycle.")
            return
        try:
            # Release lock before calling clean_disk so clean_disk can re-acquire cleanly
            _clean_lock.release()
            clean_disk(target_path=target_path, verbose=verbose)
        except Exception as exc:
            logger.error("Background disk scrubber encountered error: %s", exc)

    thread = threading.Thread(target=_worker, name="unisync-disk-scrubber", daemon=True)
    thread.start()
    return thread
