"""Unit and API integration tests for Camoufox stealth browser service."""

import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from unisync.api.main import app
from unisync.services.browser import CamoufoxService, get_camoufox_service
from unisync.api.sync_engine import global_cookie_jar, get_cookies_for_domain


@pytest.fixture
def client():
    return TestClient(app)


def test_camoufox_service_status():
    """Verify that CamoufoxService correctly reports installation and profile paths."""
    svc = get_camoufox_service()
    status = svc.get_status()
    assert "installed" in status
    assert status["installed"] is True
    assert "profile_dir" in status
    assert "has_persistent_profile" in status
    assert "cookies_in_memory" in status


def test_browser_api_status(client):
    """Verify GET /api/v1/browser/status returns the correct schema."""
    res = client.get("/api/v1/browser/status")
    assert res.status_code == 200
    data = res.json()
    assert data["installed"] is True
    assert "profile_dir" in data
    assert "canvas_auth_status" in data


def test_sync_status_includes_camoufox(client):
    """Verify GET /api/v1/sync/status embeds the camoufox diagnostic payload."""
    res = client.get("/api/v1/sync/status")
    assert res.status_code == 200
    data = res.json()
    assert "camoufox" in data
    assert data["camoufox"]["installed"] is True


def test_cookie_jar_format_compatibility():
    """Verify that cookies harvested in (domain, name) tuple format work with get_cookies_for_domain."""
    test_cookies = [
        {"name": "_canvas_session", "value": "secret_session_token_123", "domain": "canvas.lms.unimelb.edu.au"},
        {"name": "log_in_to_canvas_net", "value": "1", "domain": ".unimelb.edu.au"},
        {"name": "ECHO_JWT", "value": "echo_jwt_token_456", "domain": "echo360.net.au"},
    ]
    
    # Simulate Camoufox harvesting
    for c in test_cookies:
        global_cookie_jar[(c["domain"], c["name"])] = c["value"]

    canvas_cookies = get_cookies_for_domain("canvas.lms.unimelb.edu.au")
    assert canvas_cookies.get("_canvas_session") == "secret_session_token_123"
    assert canvas_cookies.get("log_in_to_canvas_net") == "1"

    echo_cookies = get_cookies_for_domain("echo360.net.au")
    assert echo_cookies.get("ECHO_JWT") == "echo_jwt_token_456"


@pytest.mark.asyncio
async def test_interactive_login_mock(tmp_path):
    """Verify interactive login flow with mocked AsyncCamoufox."""
    svc = CamoufoxService(profile_dir=tmp_path)
    
    mock_page = AsyncMock()
    mock_page.url = "https://canvas.lms.unimelb.edu.au/"
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.evaluate.return_value = {}
    
    mock_context = AsyncMock()
    mock_context.pages = [mock_page]
    mock_context.cookies.return_value = [
        {"name": "canvas_test", "value": "val123", "domain": "canvas.lms.unimelb.edu.au"}
    ]
    mock_context.request.get.return_value.status = 200
    mock_context.request.get.return_value.json.return_value = {"id": 123}
    mock_context.storage_state.return_value = {"cookies": [], "origins": []}
    mock_context.on = MagicMock()
    mock_context.browser = MagicMock()
    mock_context.browser.is_connected.return_value = True
    
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_context
    mock_cm.__aexit__.return_value = None

    with patch("camoufox.async_api.AsyncCamoufox", return_value=mock_cm), \
         patch.object(svc, "_launch_options", return_value={}):
        res = await svc.interactive_login(timeout_seconds=5)
        assert res["success"] is True
        assert res["cookies_count"] == 1
        assert global_cookie_jar.get(("canvas.lms.unimelb.edu.au", "canvas_test")) == "val123"
