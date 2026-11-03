# Camoufox persistence audit — 2026-10-30

This document records the audit before the resident-browser follow-up. Items
1–4 below are now implemented; see [current behavior](CANVAS_SESSION_SURVIVAL.md).
Long-duration validation remains open. The audited implementation was a tested
baseline, not the maximum possible session survival. Its live browser-relaunch and backend-restart tests establish short-term
recovery, not indefinite authentication or survival across every SSO policy.

## Review scope and installed versions

Reviewed official Camoufox usage, configuration, remote-server and fingerprint
documentation; upstream Python launch/fingerprint source and browser preferences;
the installed launcher and browser defaults; Playwright authentication, persistent
context, storage-state and shared-request-cookie documentation; and Okta's own
session-lifetime/persistent-cookie documentation. Relevant links are below.

Installed: Camoufox Python 0.5.6; browser 152.0.4-beta.30; Playwright 1.61.0;
BrowserForge 1.2.4. Upstream main and the latest Playwright website describe newer
features too. Local signatures were checked rather than assuming every website
example applies: `storage_state(indexed_db=True)` and `set_storage_state` exist;
the installed storage-state method has no `opfs` or `credentials` argument.

UniSync uses the Camoufox Python launcher directly. Settings such as
`managed_persistence`, `CAMOFOX_PROFILE_DIR` and a `persistence` plugin belong to
other Camofox wrappers and are not configuration switches for this installation.

## Comparison with UniSync

| Area | Current implementation | Assessment |
|---|---|---|
| Persistent Firefox profile | Fixed `user_data_dir`, `persistent_context=True` | Implemented |
| Generated device identity | Saves launch configuration and reuses it across visible/headless runs | Implemented within one browser version |
| Cookies across launches | Atomic checkpoint, restores missing unexpired cookies, preserves original expiry | Implemented |
| Login verification | Canvas JSON profile response with an ID | Implemented |
| Profile concurrency | One asyncio lock for browser operations | Implemented within the server process |
| Session maintenance | Startup check and ten-minute checks while the backend runs | Implemented; not proof of extending SSO lifetime |
| Local storage / IndexedDB | Retained by the existing Firefox profile | Present in profile; no independent recovery snapshot |
| Live page and sessionStorage | Browser closes after each operation | Remaining gap |
| Cookie changes from native HTTP sync | HTTPX clients receive copied cookies | Remaining gap: changed cookies are not returned to the browser/checkpoint |
| Identity across browser updates | Whole generated identity is replaced when browser version changes | Opportunity for version-aware migration |
| Crash/sleep/overnight evidence | Unit tests and short live restart checks | Longer-duration validation still needed |

## Recommended order

1. **Reuse one live browser context and a Canvas page.** Keep them owned by the
   backend lifecycle, with serialized operations, crash recovery and an orderly
   checkpoint/close. This is an engineering recommendation inferred from context
   lifetime and sessionStorage behavior, not a Camoufox guarantee about Canvas.
   It avoids repeatedly discarding live page state. A resident browser costs more
   memory. A visible login may require an orderly transition from a headless
   context because launch mode cannot simply be toggled on an existing process.

2. **Use one authoritative cookie jar.** Canvas API requests through
   `browser_context.request` share the browser's cookie storage and receive
   response cookie changes there. UniSync's separate HTTPX clients currently copy
   a simplified cookie map, losing expiry/path details and providing no cookie
   update path back. This is a concrete code gap; no claim is made that Canvas
   rotates its authentication cookie on every request. Keep large attachment
   downloads streamed rather than buffering them through APIResponse bodies.

3. **Add full recovery checkpoints.** `storage_state(indexed_db=True)` can back up
   cookies, local storage and IndexedDB. It supplements the existing profile;
   local storage is not currently absent. Restore as a recovery operation, not
   blindly on every launch: `set_storage_state` clears existing storage first and
   could overwrite newer state. Include snapshot generations and corruption
   recovery. Capture relevant origin sessionStorage separately only if actual
   Canvas/Okta use is established; Playwright does not include it automatically.

4. **Migrate device identity across updates.** Preserve device characteristics
   where compatible, while updating browser-version-dependent fields and paths.
   The current full regeneration on any version change can introduce a new
   device identity. Test real runtime identity across visible/headless launches,
   not only equality of saved options.

5. **Measure real survival.** Add cookie-free diagnostics separating Canvas
   authentication, SSO recovery and server refusal. Validate cookie rotation,
   crash recovery, Windows sleep/wake, connection loss, browser upgrades and
   overnight use. The previous 260 passing tests remain valid but are not proof
   of a maximum attainable session duration.

## Findings that limit the meaning of “maximum”

- Canvas and Okta have separate session state. A successful Canvas profile call
  does not establish that Okta recognized activity or extended its idle timer.
  The ten-minute interval is an implementation choice, not a vendor-documented
  universal renewal interval.
- Okta's maximum lifetime runs from sign-in even while the user is active. Its
  idle lifetime is a separate policy. UniSync cannot change university policy.
- If the university offers Keep me signed in, it can help under the configured
  policy, but that choice and policy availability have not been verified here.
- The installed browser defaults disable normal tab/crash-session restoration
  and use `browser.sessionstore.privacy_level=2`. Persistent cookies/site storage
  and restoring a live tab session are different mechanisms. Avoid assuming the
  persistent-context flag restores everything, or enabling every sessionstore
  preference without testing its effect on automation.
- Camoufox documents a websocket server but labels it experimental. A resident
  context inside the existing backend can address process churn without adopting
  that separate server.

These recommendations informed the resident-browser follow-up documented in
CANVAS_SESSION_SURVIVAL.md. Runtime behavior is described there.

## Primary sources

- [Camoufox usage and persistent profile](https://camoufox.com/python/usage/)
- [Camoufox configuration](https://camoufox.com/python/config/)
- [Camoufox fingerprint documentation](https://camoufox.com/fingerprint/)
- [Camoufox remote server](https://camoufox.com/python/remote-server/)
- [Camoufox Python README](https://github.com/daijro/camoufox/blob/main/python/README.md)
- [Async launcher and context close behavior](https://github.com/daijro/camoufox/blob/main/python/src/camoufox/async_api.py)
- [Fingerprint identity handling](https://github.com/daijro/camoufox/blob/main/python/src/camoufox/fingerprints.py)
- [Browser preference defaults](https://github.com/daijro/camoufox/blob/main/browser/settings/camoufox.cfg)
- [Upstream session-reuse issue 442](https://github.com/daijro/camoufox/issues/442)
- [Playwright persistent-context API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [Playwright authentication and sessionStorage](https://playwright.dev/python/docs/auth)
- [Playwright storage-state API](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-storage-state)
- [Playwright shared API-request cookie jar](https://playwright.dev/python/docs/api/class-apirequestcontext)
- [Okta maximum lifetime and idle-time calculation](https://support.okta.com/help/s/article/How-is-the-Session-Lifetime-calculated)
- [Okta persistent-session requirements](https://support.okta.com/help/s/article/understanding-the-behavior-of-enabling-okta-global-session-cookies-persist-across-browser-sessions?language=en_US)
