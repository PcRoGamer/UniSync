import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from unisync.services.browser import CamoufoxService
from unisync.services.browser.canvas_transport import CanvasSessionTransport, apply_response_cookies


def browser_context(status=200):
    context = AsyncMock()
    context.on = MagicMock()
    context.browser = MagicMock()
    context.browser.is_connected.return_value = True
    context.cookies.return_value = []
    context.storage_state.return_value = {'cookies': [], 'origins': []}
    context.request.get.return_value.status = status
    context.request.get.return_value.json.return_value = {'id': 1}
    context.request.get.return_value.headers = {}
    page = MagicMock()
    page.url = 'https://canvas.lms.unimelb.edu.au/'
    page.is_closed.return_value = False
    page.evaluate = AsyncMock(return_value={})
    context.pages = [page]
    return context


@pytest.mark.asyncio
async def test_resident_reuses_then_recovers_and_closes(tmp_path):
    service = CamoufoxService(tmp_path)
    first, recovered = browser_context(), browser_context()
    managers = [AsyncMock(), AsyncMock()]
    for manager, context in zip(managers, [first, recovered]):
        manager.__aenter__.return_value = context
    with patch('camoufox.async_api.AsyncCamoufox', side_effect=managers) as launch, \
         patch.object(service, '_launch_options', return_value={}):
        assert (await service.background_sync_session())['authenticated']
        assert (await service.background_sync_session())['authenticated']
        assert launch.call_count == 1
        managers[0].__aexit__.assert_not_awaited()
        # Model a user closing the browser / a browser crash.
        first.on.call_args.args[1]()
        first.browser.is_connected.return_value = False
        assert (await service.background_sync_session())['authenticated']
        assert launch.call_count == 2
        await service.close()
        managers[1].__aexit__.assert_awaited_once()
        assert service._live_context is None


@pytest.mark.asyncio
async def test_headful_to_background_reuses_same_browser(tmp_path):
    service = CamoufoxService(tmp_path)
    manager = AsyncMock()
    manager.__aenter__.return_value = browser_context()
    with patch('camoufox.async_api.AsyncCamoufox', return_value=manager) as launch, \
         patch.object(service, '_launch_options', return_value={}):
        assert (await service.interactive_login())['success']
        assert (await service.background_sync_session())['authenticated']
        assert launch.call_count == 1
        await service.close()


@pytest.mark.asyncio
async def test_headless_login_transition_checkpoints_before_visible_launch(tmp_path):
    service = CamoufoxService(tmp_path)
    managers = [AsyncMock(), AsyncMock()]
    for manager in managers:
        manager.__aenter__.return_value = browser_context()
    with patch('camoufox.async_api.AsyncCamoufox', side_effect=managers) as launch, \
         patch.object(service, '_launch_options', return_value={}):
        assert (await service.background_sync_session())['authenticated']
        assert (await service.interactive_login())['success']
        managers[0].__aexit__.assert_awaited_once()
        assert service._session_path.with_suffix('.bak').exists()
        assert launch.call_count == 2
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_blocks_queued_browser_relaunches(tmp_path):
    service = CamoufoxService(tmp_path)
    await service.close(permanent=True)
    with patch('camoufox.async_api.AsyncCamoufox') as launch:
        with pytest.raises(RuntimeError, match='shutting down'):
            async with service._context(True):
                pass
        launch.assert_not_called()


@pytest.mark.asyncio
async def test_full_snapshot_recovery_only_on_invalid_profile(tmp_path):
    service = CamoufoxService(tmp_path)
    state = {'cookies': [], 'origins': [{'origin': 'https://canvas.lms.unimelb.edu.au',
                                        'localStorage': [{'name': 'app', 'value': 'saved'}],
                                        'indexedDB': []}],
             'session_storage': {'https://canvas.lms.unimelb.edu.au': {'tab': 'retained'}}}
    service._write_private(service._session_path, state)
    healthy = browser_context()
    await service._restore(healthy)
    healthy.set_storage_state.assert_not_awaited()
    healthy.add_init_script.assert_awaited_once()
    assert 'sessionStorage.length === 0' in healthy.add_init_script.call_args.kwargs['script']
    invalid = browser_context(401)
    await service._restore(invalid)
    invalid.set_storage_state.assert_awaited_once_with({'cookies': [], 'origins': state['origins']})
    unavailable = browser_context(503)
    await service._restore(unavailable)
    unavailable.set_storage_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_checkpoint_and_previous_good_generation(tmp_path):
    service = CamoufoxService(tmp_path)
    context = browser_context()
    context.storage_state.return_value = {'cookies': [], 'origins': [{'origin': 'https://canvas.lms.unimelb.edu.au',
                                          'localStorage': [], 'indexedDB': [{'name': 'auth', 'version': 1}]}]}
    await service._checkpoint(context)
    context.storage_state.assert_awaited_with(indexed_db=True)
    await service._checkpoint(context)
    service._session_path.write_text('corrupt')
    snapshot = service._load_snapshot(record_source=True)
    assert snapshot['origins'][0]['indexedDB'][0]['name'] == 'auth'
    assert service._recovery_source == 'backup'


def test_browser_upgrade_keeps_hardware_configuration(tmp_path):
    service = CamoufoxService(tmp_path)
    old_config = {'navigator.userAgent': 'old-browser', 'navigator.appVersion': 'old',
                  'navigator.hardwareConcurrency': 8, 'screen.width': 1920,
                  'webGl:renderer': 'same-gpu', 'canvas:seed': 123}
    old = {'version': 'old', 'options': {'env': {'CAMOU_CONFIG_1': json.dumps(old_config)},
                                        'firefox_user_prefs': {}}}
    service._write_private(service._identity_path, old)
    generated = {'env': {'CAMOU_CONFIG_1': '{}'}, 'firefox_user_prefs': {}, 'executable_path': 'new.exe'}
    with patch('camoufox.pkgman.installed_verstr', return_value='new'), \
         patch('camoufox.utils.launch_options', return_value=generated) as build:
        service._launch_options(True)
        config = build.call_args.kwargs['config']
        assert config['navigator.hardwareConcurrency'] == 8
        assert config['canvas:seed'] == 123
        assert config['webGl:renderer'] == 'same-gpu'
        assert 'navigator.userAgent' not in config
        assert service._identity_path.with_suffix('.bak').exists()


def transport_service(context):
    service = MagicMock()
    service._mirror_cookies = AsyncMock()
    service.flush = AsyncMock()
    @asynccontextmanager
    async def lease(**kwargs):
        yield context
    service._context = lease
    return service


@pytest.mark.asyncio
async def test_json_requests_share_cookie_owner_and_decode_once():
    context = browser_context()
    response = context.request.fetch.return_value
    response.status = 200
    response.headers_array = [{'name': 'Content-Type', 'value': 'application/json'},
                              {'name': 'Content-Encoding', 'value': 'gzip'}]
    response.body.return_value = b'{"id":1}'
    service = transport_service(context)
    async with httpx.AsyncClient(transport=CanvasSessionTransport(service), cookies={'session': 'stale'}) as client:
        for _ in range(2):
            result = await client.get('https://canvas.lms.unimelb.edu.au/api/v1/users/self/profile')
            assert result.json()['id'] == 1
            assert 'cookie' not in context.request.fetch.call_args.kwargs['headers']
    assert context.request.fetch.await_count == 2
    assert service._mirror_cookies.await_count == 2
    service.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_cookie_rotation_is_mirrored_from_the_same_context(tmp_path):
    from unisync.api.sync_engine import global_cookie_jar
    context = browser_context()
    cookies = [{'domain': 'canvas.lms.unimelb.edu.au', 'name': 'session', 'value': 'old', 'path': '/'}]
    context.cookies.side_effect = lambda *args: cookies
    seen = []
    async def fetch(*args, **kwargs):
        seen.append(cookies[0]['value'])
        cookies[0]['value'] = 'rotated'
        response = MagicMock()
        response.status = 200
        response.headers_array = []
        response.body = AsyncMock(return_value=b'{}')
        response.dispose = AsyncMock()
        return response
    context.request.fetch.side_effect = fetch
    service = CamoufoxService(tmp_path)
    @asynccontextmanager
    async def lease(**kwargs):
        yield context
    service._context = lease
    with patch.dict(global_cookie_jar, {}, clear=True):
        async with httpx.AsyncClient(transport=CanvasSessionTransport(service)) as client:
            for _ in range(2):
                await client.get('https://canvas.lms.unimelb.edu.au/api/v1/courses')
        assert seen == ['old', 'rotated']
        assert global_cookie_jar[('canvas.lms.unimelb.edu.au', 'session')] == 'rotated'


@pytest.mark.asyncio
async def test_stream_redirect_does_not_leak_canvas_cookie_and_returns_rotation():
    context = browser_context()
    async def cookies(urls=None):
        if urls and 'canvas.lms.unimelb.edu.au' in urls[0]:
            return [{'name': 'session', 'value': 'browser-latest'}]
        return []
    context.cookies.side_effect = cookies
    hops = []
    def handle(request):
        hops.append(request)
        if request.url.host == 'canvas.lms.unimelb.edu.au':
            assert request.headers['cookie'] == 'session=browser-latest'
            return httpx.Response(302, headers={'location': 'https://cdn.example/file',
                'set-cookie': 'session=rotated; Secure; HttpOnly; Path=/; SameSite=Lax'})
        assert 'cookie' not in request.headers
        return httpx.Response(200, content=b'streamed-file')
    service = transport_service(context)
    transport = CanvasSessionTransport(service, httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        async with client.stream('GET', 'https://canvas.lms.unimelb.edu.au/files/1/download') as response:
            assert await response.aread() == b'streamed-file'
    assert len(hops) == 2
    stored = context.add_cookies.call_args.args[0][0]
    assert stored['value'] == 'rotated'
    assert stored['domain'] == 'canvas.lms.unimelb.edu.au'
    assert stored['secure'] and stored['httpOnly']


@pytest.mark.asyncio
async def test_stream_cookie_deletion_and_forged_domain():
    context = browser_context()
    response = httpx.Response(200, headers=[
        ('set-cookie', 'session=; Max-Age=0; Path=/'),
        ('set-cookie', 'forged=value; Domain=canvas.lms.unimelb.edu.au; Path=/')],
        request=httpx.Request('GET', 'https://cdn.example/file'))
    await apply_response_cookies(context, response)
    context.clear_cookies.assert_awaited_once_with(name='session', domain='cdn.example', path='/')
    context.add_cookies.assert_not_awaited()
