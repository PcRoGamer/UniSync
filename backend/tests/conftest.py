"""Run UniSync's tests in an entirely separate disposable data root.

Never let an integration test register temporary downloads, dummy courses,
emails or tasks in the real user's UniSync manifest. The per-process
UNISYNC_DATA_DIR is established before test modules are imported.
"""
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

_TEST_DATA_ROOT = None
_PRIOR_DATA_ROOT = None


def pytest_configure(config):
    global _TEST_DATA_ROOT, _PRIOR_DATA_ROOT
    _PRIOR_DATA_ROOT = os.environ.get("UNISYNC_DATA_DIR")
    _TEST_DATA_ROOT = tempfile.mkdtemp(prefix="unisync-pytest-isolated-")
    os.environ["UNISYNC_DATA_DIR"] = _TEST_DATA_ROOT


def pytest_unconfigure(config):
    from unisync.core import path_service
    path_service._INSTANCE = None
    if _PRIOR_DATA_ROOT is None:
        os.environ.pop("UNISYNC_DATA_DIR", None)
    else:
        os.environ["UNISYNC_DATA_DIR"] = _PRIOR_DATA_ROOT
    if _TEST_DATA_ROOT:
        shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_browser_maintenance():
    with patch('unisync.api.browser.maintain_canvas_session', new_callable=AsyncMock), \
         patch('unisync.api.browser.shutdown_browser', new_callable=AsyncMock):
        yield
