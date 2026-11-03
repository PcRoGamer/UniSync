"""UniSync Document and File Safety Validator."""

import re
from typing import Any, Optional


class DocumentValidator:
    """Validates and sanitizes upload filenames."""

    @staticmethod
    def validate_upload_safety(filename: str, max_size: Optional[int] = None) -> str:
        # Strip directory traversal characters
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', filename).strip()
        cleaned = re.sub(r'^\.+', '', cleaned)
        return cleaned or "downloaded_file.bin"
