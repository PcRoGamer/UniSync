import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unisync.services.browser import CamoufoxService
from unisync.api.browser import maintain_canvas_session


def context_with_response(status, payload=None, headers=None):
    context = AsyncMock()
    response = context.request.get.return_value
    response.status = status
    response.headers = headers or {}
    response.json.return_value = payload
    context.cookies.return_value = []
    context.storage_state.return_value = {'cookies': [], 'origins': []}
    context.on = MagicMock()
    context.browser = MagicMock()
    context.browser.is_connected.return_value = True
    page = MagicMock()
    page.url = 'https://canvas.lms.unimelb.edu.au/'
    page.is_closed.return_value = False
    page.evaluate = AsyncMock(return_value={})
    page.goto = AsyncMock()
    context.pages = [page]
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize('status,payload,headers,expected', [
    (200, {'id': 123}, {}, True),
    (200, {}, {}, None),
    (401, {}, {}, False),
    (403, {}, {}, None),
    (503, {}, {}, None),
    (302, {}, {'location': '/login/saml'}, False),
])
async def test_auth_requires_canvas_proof(tmp_path, status, payload, headers, expected):
    service = CamoufoxService(tmp_path)
    context = context_with_response(status, payload, headers)
    assert await service._probe(context) is expected
    context.request.get.return_value.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_cookie_survives_new_service_without_overwriting_rotated_cookie(tmp_path):
    service = CamoufoxService(tmp_path)
    context = context_with_response(200, {'id': 123})
    session = {'domain': 'canvas.example', 'name': 'session', 'value': 'session-only',
               'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}
    persistent = {**session, 'name': 'rotated', 'value': 'old', 'expires': time.time() + 3600}
    expired = {**session, 'name': 'expired', 'expires': time.time() - 1}
    context.cookies.return_value = [session, persistent, expired]
    await service._checkpoint(context)
    reloaded = CamoufoxService(tmp_path)
    fresh = context_with_response(200, {'id': 123})
    fresh.cookies.return_value = [{**persistent, 'value': 'new'}]
    await reloaded._restore(fresh)
    fresh.add_cookies.assert_awaited_once_with([session])
    assert json.loads(reloaded._session_path.read_text())['cookies'][0]['expires'] == -1


@pytest.mark.asyncio
async def test_corrupt_checkpoint_does_not_break_profile(tmp_path):
    service = CamoufoxService(tmp_path)
    service._session_path.write_text('broken')
    context = context_with_response(200, {'id': 123})
    await service._restore(context)
    context.add_cookies.assert_not_called()


def test_identity_stays_consistent_without_persisting_process_secrets(tmp_path):
    service = CamoufoxService(tmp_path)
    options = {'env': {'CAMOU_CONFIG_1': 'stable-identity', 'SECRET_TOKEN': 'do-not-store'},
               'firefox_user_prefs': {}, 'executable_path': 'camoufox.exe'}
    with patch('camoufox.pkgman.installed_verstr', return_value='test-version'), \
         patch('camoufox.utils.launch_options', return_value=options) as generate:
        first = service._launch_options(False)
        second = CamoufoxService(tmp_path)._launch_options(True)
        assert generate.call_count == 1
        assert first['env']['CAMOU_CONFIG_1'] == second['env']['CAMOU_CONFIG_1']
        assert second['headless'] is True
        assert first['headless'] is False
        assert 'SECRET_TOKEN' not in service._identity_path.read_text()


@pytest.mark.asyncio
async def test_profile_launches_are_serialized(tmp_path):
    service = CamoufoxService(tmp_path)
    context = context_with_response(200, {'id': 123})
    manager = AsyncMock()
    manager.__aenter__.return_value = context
    active = 0

    async def visit():
        nonlocal active
        async with service._context(True):
            active += 1
            assert active == 1
            await asyncio.sleep(0)
            active -= 1

    with patch('camoufox.async_api.AsyncCamoufox', return_value=manager), \
         patch.object(service, '_launch_options', return_value={}):
        await asyncio.gather(visit(), visit())


@pytest.mark.asyncio
async def test_refresh_restores_without_reopening_login_page(tmp_path):
    service = CamoufoxService(tmp_path)
    context = context_with_response(200, {'id': 123})
    manager = AsyncMock()
    manager.__aenter__.return_value = context
    with patch('camoufox.async_api.AsyncCamoufox', return_value=manager), \
         patch.object(service, '_launch_options', return_value={}):
        result = await service.background_sync_session()
    assert result['authenticated'] is True
    assert not result['login_required']
    context.new_page.assert_not_called()


@pytest.mark.asyncio
async def test_network_error_does_not_launch_login():
    from unisync.api.browser import refresh_browser_session
    with patch('unisync.api.browser.get_camoufox_service') as service, \
         patch('unisync.api.browser.trigger_interactive_login', new_callable=AsyncMock) as login:
        service.return_value.is_installed.return_value = True
        service.return_value.background_sync_session = AsyncMock(return_value={
            'authenticated': False, 'login_required': False})
        result = await refresh_browser_session()
        assert not result['login_required']
        login.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintenance_restores_at_startup_then_waits_ten_minutes():
    with patch('unisync.api.browser.get_camoufox_service') as factory, \
         patch('unisync.api.browser.asyncio.sleep', new_callable=AsyncMock,
               side_effect=asyncio.CancelledError) as sleep:
        factory.return_value.get_status.return_value = {'installed': True, 'has_persistent_profile': True}
        factory.return_value.background_sync_session = AsyncMock(return_value={'authenticated': True})
        with pytest.raises(asyncio.CancelledError):
            await maintain_canvas_session()
        factory.return_value.background_sync_session.assert_awaited_once()
        sleep.assert_awaited_once_with(30)
