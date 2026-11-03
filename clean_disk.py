#!/usr/bin/env python3
"""UniSync On-Disk Course Material Sanitizer CLI.

Scans local markdown and text files across data/courses and data/cache,
stripping institutional academic integrity disclaimers, Turnitin notices,
and AI policy boilerplate in-place.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add backend to sys.path
root = Path(__file__).resolve().parent
backend_dir = root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from unisync.services.disk_cleaner import clean_disk


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    target_str = str(target) if target else "data/courses and data/cache"
    print(f"[*] Scanning {target_str} for academic policy boilerplate...", flush=True)
    scanned, modified = clean_disk(target_path=target, verbose=True)
    print(f"[+] Complete: {scanned} files scanned, {modified} files sanitized in-place.", flush=True)


if __name__ == "__main__":
    main()
