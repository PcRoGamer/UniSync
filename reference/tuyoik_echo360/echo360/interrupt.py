import os
import signal
import sys

_active_downloaders = []
_sigint_count = 0
_installed = False


def register_downloader(downloader):
    _active_downloaders.append(downloader)


def unregister_downloader(downloader):
    try:
        _active_downloaders.remove(downloader)
    except ValueError:
        pass


def _handle_sigint(signum, frame):
    global _sigint_count
    _sigint_count += 1
    for downloader in list(_active_downloaders):
        try:
            downloader._abort_download()
        except Exception:
            pass
    if _sigint_count >= 2:
        print("\n>> Force quit.")
        os._exit(130)
    print("\n>> Cancelling... (press Ctrl+C again to force quit)")
    raise KeyboardInterrupt()


def install_sigint_handler():
    global _installed
    if _installed:
        return
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
        _installed = True
    except (ValueError, OSError):
        pass
