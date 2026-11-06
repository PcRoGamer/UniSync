"""Keep lifecycle tests isolated from the user's live university session."""
from importlib import import_module
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolate_browser_maintenance():
    # In lightweight CI, the optional Camoufox/browser runtime may exist as a
    # source file but fail import because browser dependencies are not installed.
    # Email-only tests must not depend on that optional component.
    try:
        browser = import_module("unisync.api.browser")
    except ImportError:
        yield
        return
    with patch.object(browser, "maintain_canvas_session", new_callable=AsyncMock), \
         patch.object(browser, "shutdown_browser", new_callable=AsyncMock):
        yield
