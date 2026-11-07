"""Zero-dependency stdio wake relay for UniSync.

The OpenAI tunnel runs this lightweight process immediately, but the heavy
UniSync MCP server is started only after the first tunneled protocol byte.
It forwards bytes transparently; scrubbing remains in unisync.mcp.server.
Never write diagnostic text to stdout, which is reserved for MCP JSON-RPC.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading


def main() -> int:
    try:
        first = os.read(sys.stdin.fileno(), 1)
    except OSError:
        return 1
    if not first:  # No MCP request arrived during the tunnel's lifetime.
        return 0

    command = [sys.executable, "-m", "unisync.mcp.server"]
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            cwd=os.getcwd(),
            bufsize=0,
        )
    except OSError:
        return 1

    def forward_requests() -> None:
        try:
            assert child.stdin is not None
            child.stdin.write(first)
            while True:
                block = os.read(sys.stdin.fileno(), 16384)
                if not block:
                    break
                child.stdin.write(block)
        except (OSError, BrokenPipeError, ValueError):
            pass
        finally:
            try:
                if child.stdin:
                    child.stdin.close()
            except OSError:
                pass

    inbound = threading.Thread(target=forward_requests, name="mcp-stdin-relay", daemon=True)
    inbound.start()
    try:
        assert child.stdout is not None
        while True:
            block = os.read(child.stdout.fileno(), 16384)
            if not block:
                break
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
    except (OSError, BrokenPipeError, ValueError):
        pass
    finally:
        if child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
                child.wait()
    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
