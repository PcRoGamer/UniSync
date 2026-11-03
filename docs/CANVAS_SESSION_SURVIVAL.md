# Canvas session survival

UniSync keeps one resident Camoufox browser context and a Canvas app tab alive
while its backend runs. Login, session checks, native Canvas APIs and Echo360
resolution serialize access to the same profile. A browser close/crash causes
recovery on the next operation. A visible context can serve background work;
interactive login checkpoints and closes a headless context before opening a
visible one. It never launches two browser instances against the same profile.

## Maintenance and lifecycle

- Verify Canvas through its JSON `/api/v1/users/self/profile` response at startup
  and every ten minutes. A Canvas-looking URL alone is not login proof.
- Checkpoint locally every 30 seconds, on successful verification, when HTTP
  clients close, and before browser mode transitions or orderly shutdown.
- Temporary connectivity failures retry after one minute. HTTP 401 or an explicit
  SSO redirect permits interactive login; unexpected HTML, 403 and 5xx do not
  establish expiry. Silent SSO gets up to 45 seconds before interactive recovery.
- Shut down the maintenance/login tasks and checkpoint/close the browser on
  backend shutdown. New operations cannot relaunch it during final shutdown.

`POST /api/v1/browser/close` checkpoints and closes the browser for maintenance.
A later refresh can relaunch it. This differs from final backend shutdown, which
blocks relaunches. No OS scheduler or Codex automation is installed.

## One cookie owner

Native Canvas JSON APIs use Playwright's `context.request`, which shares the
resident browser cookie jar. Response cookie rotation is immediately visible to
later API requests and to browser navigation.

Attachment downloads continue to stream through HTTPX. For every URL, including
every redirect hop, the transport asks the browser for applicable cookies instead
of trusting HTTPX's copied cookie map. Streamed response Set-Cookie updates and
removals are applied back to the browser; cookies claiming an unrelated domain
are rejected. Signed attachment URLs are excluded from routine HTTPX logs.

## Recovery state

Private data stays under `data/browser_profile/camoufox/`, which is Git-ignored:

- `unisync-session.json`: schema 2 checkpoint containing cookies, local storage,
  IndexedDB snapshots, saved time, and observed Canvas-origin sessionStorage.
- `unisync-session.bak`: previous valid checkpoint, used if the latest is damaged.
- `unisync-identity.json`: generated browser identity without the parent process
  environment; its `.bak` retains the preceding identity during migration.

Checkpoint writes use a private temporary file, flush/fsync and atomic rename.
Cookie expiry values, HttpOnly, Secure, SameSite, domain and path are preserved.
Session cookies with expiry -1 remain session cookies; no server-set lifetime is
extended. Legacy cookie-only checkpoints still load.

Normal restore fills missing unexpired cookies. It does not overwrite newer
cookies already present in Firefox. A full storage replacement is attempted only
when Canvas rejects the restored profile and a full checkpoint exists; healthy
profiles and connectivity failures do not trigger destructive storage replacement.
Observed Canvas sessionStorage is restored only for that exact origin into empty
sessionStorage before page scripts. No arbitrary external origin is injected.

Firefox itself retains local storage, IndexedDB and other profile state. The full
checkpoint adds recovery coverage rather than claiming those stores were absent.
A resident page also retains live JavaScript and tab state between operations.

## Stable identity and updates

The same generated launch identity is reused for visible/headless modes. When a
browser update is detected, UniSync passes the saved device configuration through
the new launcher's validation/generation, refreshing navigator userAgent,
appVersion and buildID instead of randomly replacing device characteristics.
Incompatible or corrupt configuration falls back to a newly generated valid
identity. No browser/library upgrade is installed by this feature.

## Diagnostics and limits

`GET /api/v1/browser/status` reports authentication, resident-browser state,
browser launch count, checkpoint/verification times, recovery source and lock
state. It never returns cookie or storage values.

A Canvas check is not proof of renewal of the separate Okta SSO session. The
university's maximum lifetime, idle policies, revocation and MFA requirements
still apply. Restart/reuse tests do not establish indefinite session survival.
Long-duration, sleep/wake and actual browser-upgrade validation remain useful.

## Validation on 2026-10-30

All 271 backend tests passed. Focused cases cover resident reuse, crash recovery,
visible/background reuse, headless-to-visible transitions, shutdown relaunch
prevention, full-state recovery only for invalid profiles, damaged checkpoint
fallback, compatible identity migration, cookie rotation/deletion, redirect
isolation and decoded API responses.

Live checks confirmed authenticated reuse across two refreshes with browser launch
count remaining one. A real Canvas submission sync returned six attempts and four
attachments with no errors through the shared transport. The schema 2 checkpoint
contained two origins and seven observed Canvas sessionStorage entries. Graceful
close saved state; a complete backend restart recovered authenticated Canvas with
interactive-login status idle. The browser remains resident after startup.

These checks are not an overnight, sleep/wake or real browser-upgrade endurance test.

## References

- https://camoufox.com/python/usage/
- https://github.com/daijro/camoufox/blob/main/python/src/camoufox/async_api.py
- https://playwright.dev/python/docs/auth
- https://playwright.dev/python/docs/api/class-browsercontext#browser-context-storage-state
- https://playwright.dev/python/docs/api/class-apirequestcontext
