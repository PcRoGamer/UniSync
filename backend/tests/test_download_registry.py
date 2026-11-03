from __future__ import annotations

import time
from pathlib import Path

import pytest

from unisync.core.download_registry import DownloadRegistry


@pytest.fixture
def registry(tmp_path: Path) -> DownloadRegistry:
    manifest = tmp_path / "downloads_manifest.json"
    reg = DownloadRegistry(manifest)

    canvas_file = tmp_path / "banner-blaw10001-1.png"
    canvas_file.write_bytes(b"png-fixture")
    reg.register(
        platform="canvas",
        item_id="26804961",
        title="banner-blaw10001-1.png",
        direct_link="https://canvas.lms.unimelb.edu.au/courses/240718/files/26804961/download?download_frd=1",
        local_filepath=canvas_file,
        course_id="240718",
        course_name="BLAW10001",
        content_type="image/png",
    )

    subtitle = tmp_path / "lecture-fixture.vtt"
    subtitle.write_text("WEBVTT\n", encoding="utf-8")
    reg.register(
        platform="echo360",
        item_id="lecture-fixture:subtitles",
        title="lecture-fixture.vtt",
        direct_link="https://echo360.net.au/media/download/lecture-fixture/transcript.vtt",
        local_filepath=subtitle,
        course_id="echo-course",
        course_name="COMP10001",
        content_type="text/vtt",
    )
    return reg


def test_registry_loads_existing_items(registry: DownloadRegistry) -> None:
    reloaded = DownloadRegistry(registry._data_path)
    assert len(reloaded.list_all()) == 2


def test_resolve_by_bare_filename(registry: DownloadRegistry) -> None:
    item = registry.resolve("banner-blaw10001-1.png")
    assert item is not None
    assert item.platform == "canvas"
    assert item.course_id == "240718"
    assert "download_frd=1" in item.direct_link
    assert item.title == "banner-blaw10001-1.png"


def test_resolve_by_direct_link(registry: DownloadRegistry) -> None:
    url = "https://canvas.lms.unimelb.edu.au/courses/240718/files/26804961/download?download_frd=1"
    item = registry.resolve(url)
    assert item is not None
    assert item.item_id == "26804961"
    assert "banner-blaw10001-1.png" in item.local_filepath


def test_resolve_by_item_id(registry: DownloadRegistry) -> None:
    item = registry.resolve("26804961")
    assert item is not None
    assert item.title == "banner-blaw10001-1.png"


def test_resolve_echo360_subtitle(registry: DownloadRegistry) -> None:
    item = registry.resolve("lecture-fixture.vtt")
    assert item is not None
    assert item.platform == "echo360"
    assert "echo360.net.au" in item.direct_link


def test_instant_lookup_benchmark(registry: DownloadRegistry) -> None:
    queries = ["banner-blaw10001-1.png", "26804961", "lecture-fixture.vtt", "non-existent-file.pdf"]
    t0 = time.perf_counter()
    for _ in range(250):
        for query in queries:
            registry.resolve(query)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 1500.0, f"1000 lookups took {elapsed_ms:.2f}ms"
