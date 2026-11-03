"""Keep lifecycle tests isolated from the user's live university session."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolate_browser_maintenance():
    with patch('unisync.api.browser.maintain_canvas_session', new_callable=AsyncMock), \
         patch('unisync.api.browser.shutdown_browser', new_callable=AsyncMock):
        yield
