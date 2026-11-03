import json

import httpx
import pytest

from unisync.services.sync.submissions import SubmissionSync


@pytest.mark.asyncio
async def test_busy_index_is_retried(tmp_path):
    from unittest.mock import AsyncMock, patch
    from unisync.services.sync.submissions import sync_submissions
    with patch.object(SubmissionSync, 'run', new_callable=AsyncMock, return_value={'attempts': 1}), \
         patch('unisync.services.kb.get_kb_service') as kb, \
         patch('unisync.services.sync.submissions.asyncio.sleep', new_callable=AsyncMock):
        kb.return_value.index_all.side_effect = [{'status': 'busy-skipped'}, {'files_indexed': 1}]
        result = await sync_submissions()
        assert result['state'] == 'done'
        assert result['index']['files_indexed'] == 1
        assert kb.return_value.index_all.call_count == 2


@pytest.mark.asyncio
async def test_expired_refresh_opens_login():
    from unittest.mock import AsyncMock, patch
    from unisync.api.browser import refresh_browser_session
    with patch('unisync.api.browser.get_camoufox_service') as service, \
         patch('unisync.api.browser.trigger_interactive_login', new_callable=AsyncMock) as login:
        service.return_value.is_installed.return_value = True
        service.return_value.background_sync_session = AsyncMock(return_value={'authenticated': False, 'login_required': True})
        assert (await refresh_browser_session())['login_required'] is True
        login.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_group_files_and_repeat_sync(tmp_path):
    downloads = []
    attachment = {'id': 91, 'filename': '../../report.txt', 'url': 'https://cdn.example/report', 'size': 11}
    first = {'attempt': 1, 'submitted_at': '2026-09-01', 'body': '<p>Our first design</p>', 'attachments': [attachment]}
    current = {'attempt': 2, 'submitted_at': '2026-09-02', 'url': 'https://figma.com/design/123',
               'submission_history': [first], 'attachments': [attachment]}

    def handle(request):
        if request.url.host == 'cdn.example':
            downloads.append(request)
            assert not request.headers.get('cookie')
            return httpx.Response(200, content=b'hello world')
        if request.url.path == '/api/v1/courses':
            if request.url.params.get('page') == '2':
                return httpx.Response(200, json=[{'id': 2, 'course_code': 'OTHER'}])
            return httpx.Response(200, json=[{'id': 1, 'course_code': 'INFO10003_2026_SM2'}],
                                  headers={'link': '<https://canvas.example/api/v1/courses?page=2>; rel="next"'})
        if request.url.path.endswith('/assignments'):
            return httpx.Response(200, json=[{'id': 3, 'name': 'Design Report', 'group_category_id': 7,
                                             'submission': first}])
        assert request.url.path.endswith('/submissions/self')
        return httpx.Response(200, json=current)

    cookies = httpx.Cookies()
    cookies.set('session', 'private', domain='canvas.example')
    async with httpx.AsyncClient(base_url='https://canvas.example', cookies=cookies,
                                 transport=httpx.MockTransport(handle)) as client:
        service = SubmissionSync(client, tmp_path)
        result = await service.run('1')
        assert result['attempts'] == 2
        assert result['failed_files'] == 0
        assert len(downloads) == 2
        summaries = sorted(tmp_path.rglob('*.md'))
        assert len(summaries) == 2
        assert 'Our first design' in summaries[0].read_text()
        assert 'https://figma.com/design/123' in summaries[1].read_text()
        assert 'Group assignment: True' in summaries[1].read_text()
        metadata = json.loads(next(tmp_path.rglob('submission.json')).read_text())
        assert metadata['files'][0]['sha256']
        assert 'https://cdn.example' not in json.dumps(metadata)
        mtimes = {p: p.stat().st_mtime_ns for p in summaries}
        await service.run('1')
        assert len(downloads) == 2
        assert mtimes == {p: p.stat().st_mtime_ns for p in summaries}
        assert all(p.is_relative_to(tmp_path) for p in tmp_path.rglob('*'))


@pytest.mark.asyncio
async def test_failed_download_retried_without_partial_files(tmp_path):
    tries = []

    def handle(request):
        tries.append(1)
        return httpx.Response(403) if len(tries) == 1 else httpx.Response(200, content=b'data')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = SubmissionSync(client, tmp_path)
        course = {'id': 1, 'course_code': 'INFO'}
        assignment = {'id': 2, 'name': 'Report'}
        submission = {'attempt': 1, 'attachments': [{'id': 3, 'filename': 'report.txt', 'url': 'https://cdn.example/file'}]}
        assert (await service.archive(course, assignment, submission))['failed'] == 1
        assert not list(tmp_path.rglob('*.part'))
        assert not list(tmp_path.rglob('*.txt'))
        assert (await service.archive(course, assignment, submission))['failed'] == 0
        assert len(list(tmp_path.rglob('*.txt'))) == 1


@pytest.mark.asyncio
async def test_reject_login_html_and_incomplete_download(tmp_path):
    responses = [httpx.Response(200, text='<html>Sign in</html>', headers={'content-type': 'text/html'}),
                 httpx.Response(200, content=b'x')]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: responses.pop(0))) as client:
        service = SubmissionSync(client, tmp_path)
        attachment = {'url': 'https://cdn.example/file', 'size': 20}
        for _ in range(2):
            assert (await service.download(attachment, tmp_path / 'report.pdf'))['status'] == 'failed'
            assert not (tmp_path / 'report.pdf').exists()
            assert not list(tmp_path.rglob('*.part'))
