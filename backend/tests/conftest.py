"""Keep lifecycle tests isolated from the user's live university session."""
from importlib.util import find_spec
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolate_browser_maintenance():
    # Older/standalone UniSync checkouts do not ship the optional browser
    # module. Do not let the global fixture invalidate unrelated email tests.
    if find_spec("unisync.api.browser") is None:
        yield
        return
    with patch('unisync.api.browser.maintain_canvas_session', new_callable=AsyncMock), \
         patch('unisync.api.browser.shutdown_browser', new_callable=AsyncMock):
        yield
