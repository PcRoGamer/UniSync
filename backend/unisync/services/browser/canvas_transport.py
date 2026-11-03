"""HTTPX transport with the resident browser as the authoritative cookie jar.

Small Canvas JSON API responses use Playwright's shared request context. Binary
downloads remain streamed through HTTPX, with domain-scoped browser cookies and
response cookie changes returned to the browser before the next request.
"""
from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie

import httpx


CANVAS_HOST = 'canvas.lms.unimelb.edu.au'


async def apply_response_cookies(context, response: httpx.Response) -> None:
    host = response.request.url.host
    path = response.request.url.path.rsplit('/', 1)[0] or '/'
    for header in response.headers.get_list('set-cookie'):
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        for name, value in parsed.items():
            domain = ('.' + value['domain'].lstrip('.')) if value['domain'] else host
            suffix = domain.lstrip('.').lower()
            if host != suffix and not host.endswith('.' + suffix):
                continue
            cookie_path = value['path'] if value['path'].startswith('/') else path
            expires = -1
            try:
                if value['max-age']:
                    expires = time.time() + int(value['max-age'])
                elif value['expires']:
                    expires = parsedate_to_datetime(value['expires']).timestamp()
            except (ValueError, TypeError, OverflowError):
                continue
            if expires != -1 and expires <= time.time():
                await context.clear_cookies(name=name, domain=domain, path=cookie_path)
                continue
            cookie = {'name': name, 'value': value.value, 'domain': domain,
                      'path': cookie_path, 'secure': bool(value['secure']),
                      'httpOnly': bool(value['httponly']), 'expires': expires}
            if value['samesite'].lower() in ('strict', 'lax', 'none'):
                cookie['sameSite'] = value['samesite'].capitalize()
            await context.add_cookies([cookie])


class CanvasSessionTransport(httpx.AsyncBaseTransport):
    def __init__(self, service=None, stream_transport=None):
        if service is None:
            from unisync.services.browser import get_camoufox_service
            service = get_camoufox_service()
        self.service = service
        self.stream_transport = stream_transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Do not let HTTPX's copied cookie jar override the current browser state.
        request.headers.pop('cookie', None)
        async with self.service._context(headless=True) as context:
            if request.url.host == CANVAS_HOST and request.url.path.startswith('/api/v1/'):
                response = await context.request.fetch(
                    str(request.url), method=request.method,
                    headers=dict(request.headers), data=(await request.aread()) or None,
                    timeout=60000, max_redirects=0, fail_on_status_code=False)
                try:
                    status = response.status
                    # Playwright returns the decoded body; HTTPX must not decode it again.
                    headers = [(h['name'], h['value']) for h in response.headers_array
                               if h['name'].lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
                    result = httpx.Response(status, headers=headers,
                                            content=await response.body(), request=request)
                    if status == 401:
                        self.service.authentication_lost()
                    await self.service._mirror_cookies(context)
                    return result
                finally:
                    await response.dispose()
            # Query the browser for cookies applicable to THIS URL on every hop,
            # rather than forwarding a Canvas Cookie header to a redirected CDN.
            cookies = await context.cookies([str(request.url)])
            if cookies:
                request.headers['cookie'] = '; '.join(f'{c["name"]}={c["value"]}' for c in cookies)
            response = await self.stream_transport.handle_async_request(request)
            response.request = request
            await apply_response_cookies(context, response)
            await self.service._mirror_cookies(context)
            return response

    async def aclose(self) -> None:
        try:
            await self.stream_transport.aclose()
        finally:
            await self.service.flush()
