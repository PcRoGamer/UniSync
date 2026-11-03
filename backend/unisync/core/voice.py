"""UniSync Voice and Transcription Module."""

import json
from typing import Any


async def transcribe_audio(chunk_bytes: bytes, filename: str = "", content_type: str = "audio/mpeg", response_format: str = "verbose_json") -> str:
    """Fallback stub when external whisper service is not running."""
    return json.dumps({"text": "", "segments": []})
