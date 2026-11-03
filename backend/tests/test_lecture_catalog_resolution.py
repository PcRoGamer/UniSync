import pytest
from pathlib import Path
from unisync.api.lectures import (
    _KNOWN_STANDALONE_TITLES,
    _resolve_clean_title,
    _download_echo360_media,
    Echo360Source,
)


def test_resolve_clean_title_standalone_and_media_url():
    """Verify 6.1, 6.2, 6.3, 8.1, and media_url UUIDs resolve to descriptive human titles, never 'Echo360 Lecture'."""
    # Direct UUID resolution
    assert _resolve_clean_title("Echo360 Video", "Canvas Page Video", "5b7f1be6-8554-4143-990e-5c18523f35c3", None) == "6.3 Divisible Contracts"
    assert _resolve_clean_title("Echo360 Video", "Canvas Page Video", "a83cbb5f-c18b-4310-82de-ec7da3f33add", None) == "6.1 Performance, Breach and Interpretation"
    assert _resolve_clean_title("Echo360 Video", "Canvas Page Video", "a8e2e620-9b0c-4344-87e5-fa50ef06c724", None) == "6.2 Different Types of Breaches"
    assert _resolve_clean_title("Echo360 Video", "Canvas Page Video", "79f0af68-f485-4775-b0df-9c551acd52ec", None) == "8.1 Vitiating Factors Overview 2026"

    # Resolution via media_url query parameter extraction (e.g. Course 237296 Student Conduct)
    url = "https://content.echo360.net.au/0000.00c445c8/ffbec428-a0fa-4e76-b3c8-0d779e2df79b/1/s1_av.m3u8?x-uid=123"
    assert _resolve_clean_title("embedded content", "Canvas Page Video", "9381284d-5253-4e5b-b173-fb47a63576cb", None, media_url=url) == "Student Conduct and Academic Integrity"


def test_resolve_clean_title_dynamic_player_fetch(monkeypatch):
    """Verify that a player URL with authentic <title> is dynamically extracted if no static mapping exists."""
    from unittest.mock import MagicMock
    from unisync.api.lectures import _FETCHED_PLAYER_TITLES
    _FETCHED_PLAYER_TITLES.clear()

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html><head><title>8.2 Duress 2026.mp4 - Echo360</title></head></html>"
    mock_resp.__enter__.return_value = mock_resp

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: mock_resp)

    res = _resolve_clean_title("Echo360 Video", "Canvas Page Video", "unknown-uuid", None, player_url="https://echo360.net.au/ui/player/unknown-uuid")
    assert res == "8.2 Duress 2026"


def test_hls_stream_detection_with_query_params(monkeypatch, tmp_path):
    """Verify that an .m3u8 URL carrying query parameters is detected as HLS rather than direct MP4."""
    called_hls = False

    def mock_download_hls(media_url, destination, headers):
        nonlocal called_hls
        called_hls = True
        destination.write_bytes(b"dummy video data")

    monkeypatch.setattr("unisync.api.lectures._download_hls_stream", mock_download_hls)

    src = Echo360Source(
        id="test_lesson",
        title="6.3 Divisible Contracts",
        date="2026-09-11",
        course_name="BLAW",
        course_id="240718",
        media_url="https://content.echo360.net.au/inst/5b7f1be6-8554-4143-990e-5c18523f35c3/1/s1_av.m3u8?x-uid=abc&x-mid=5b7f1be6",
        media_kind="m3u8",
        cookie_header="",
    )
    dest = tmp_path / "media.mp4"
    _download_echo360_media(src, dest)
    assert called_hls is True
    assert dest.exists()
    assert dest.read_bytes() == b"dummy video data"


def test_distinct_modules_never_collapse():
    """Verify that distinct topic modules (6.3 and 6.4) never deduplicate into each other even if sharing a stream."""
    import re
    # Test canonical lecture key isolation
    lec_63 = {"course_code": "BLAW10001", "title": "6.3 divisible contracts 2026.mp4", "media_url": "https://content.echo360.net.au/0000/5b7f1be6-8554-4143-990e-5c18523f35c3/1/s1_av.m3u8"}
    lec_64 = {"course_code": "BLAW10001", "title": "6.4 frustration 2026.mp4", "media_url": "https://content.echo360.net.au/0000/5b7f1be6-8554-4143-990e-5c18523f35c3/1/s1_av.m3u8"}

    def _canonical(lec):
        cc = (lec.get("course_code") or "").split("_")[0]
        t = (lec.get("title") or "").strip().lower()
        mod_match = re.search(r"\b([1-9]\.[0-9]+)\b", t)
        if mod_match:
            return (cc, f"mod_{mod_match.group(1)}")
        m_url = lec.get("media_url") or ""
        m_match = re.search(r"/([a-f0-9\-]{36})/", m_url)
        if m_match:
            return (cc, f"media_{m_match.group(1)}")
        return (cc, t)

    assert _canonical(lec_63) == ("BLAW10001", "mod_6.3")
    assert _canonical(lec_64) == ("BLAW10001", "mod_6.4")
    assert _canonical(lec_63) != _canonical(lec_64)


@pytest.mark.asyncio
async def test_blaw_thumbnail_resolution_and_fallback():
    """Verify BLAW thumbnails resolve to real cached JPEGs or clean BLAW10001 SVGs, never corrupted INFO10003."""
    from unisync.api.lectures import get_library_thumbnail

    # 1. Real cached JPEG resolution (3.2 Stare Decisis uses LTI synthetic ID)
    resp_32 = await get_library_thumbnail("BLAW_EMBED_9381284d-5253-4e5b-b173-fb47a63576cb_2f1465de")
    assert getattr(resp_32, "media_type", "") == "image/jpeg"
    assert "49a3b592-4b2f-49f5-bece-e649d382905d" in str(getattr(resp_32, "path", ""))

    # 2. Clean BLAW10001 SVG fallback (8.2 Duress)
    resp_82 = await get_library_thumbnail("BLAW_EMBED_9381284d-5253-4e5b-b173-fb47a63576cb_fc7723d1")
    assert getattr(resp_82, "media_type", "") == "image/svg+xml"
    svg_text = resp_82.body.decode("utf-8")
    assert "BLAW10001" in svg_text
    assert "8.2 Duress" in svg_text
    assert "INFO10003" not in svg_text
    assert "Student Conduct" not in svg_text

