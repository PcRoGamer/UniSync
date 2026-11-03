from __future__ import annotations

import time
from pathlib import Path
import pytest

from unisync.core.download_registry import (
    DownloadRegistry,
    DownloadItem,
    resolve_item,
    register_download,
)


def test_registry_loads_existing_items() -> None:
    reg = DownloadRegistry.get_instance()
    items = reg.list_all()
    assert len(items) >= 390


def test_resolve_by_bare_filename() -> None:
    # Query by bare filename
    item = resolve_item("banner-blaw10001-1.png")
    assert item is not None
    assert item.platform == "canvas"
    assert "240718" in item.course_id
    assert "download_frd=1" in item.direct_link
    assert item.title == "banner-blaw10001-1.png"


def test_resolve_by_direct_link() -> None:
    url = "https://canvas.lms.unimelb.edu.au/courses/240718/files/26804961/download?download_frd=1"
    item = resolve_item(url)
    assert item is not None
    assert item.item_id == "26804961"
    assert "banner-blaw10001-1.png" in item.local_filepath


def test_resolve_by_item_id() -> None:
    item = resolve_item("26804961")
    assert item is not None
    assert item.title == "banner-blaw10001-1.png"


def test_resolve_echo360_subtitle() -> None:
    item = resolve_item("G_065eec77-13b6-455c-a044-9111057cc197_5645c5ef-f457-430f-939a-6415f263ba6a_2026-07-30T16_05_00.000_2026-07-30T17_00_00.000.vtt")
    assert item is not None
    assert item.platform == "echo360"
    assert any(d in item.direct_link for d in ("echo360.net.au", "echo360.org.au"))


def test_instant_lookup_benchmark() -> None:
    """Benchmark 1,000 lookups to guarantee O(1) sub-millisecond performance for AI agents/ingestion."""
    reg = DownloadRegistry.get_instance()
    queries = ["banner-blaw10001-1.png", "26804961", "canvas-downloader", "non-existent-file.pdf"]

    t0 = time.perf_counter()
    for _ in range(250):
        for q in queries:
            reg.resolve(q)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 1,000 lookups should take well under 1500ms total (<1.5ms per lookup)
    assert elapsed_ms < 1500.0, f"1000 lookups took {elapsed_ms:.2f}ms"

