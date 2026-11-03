"""UniSync Core LLM Client.

Provides an isolated, provider-agnostic completion interface for email extraction,
summarization, and metadata tagging without any external project dependencies.
Supports:
- OpenAI / OpenAI-compatible API (e.g. Ollama, LMStudio, vLLM)
- Fallback heuristic/mock responses when offline
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
import httpx

logger = logging.getLogger(__name__)


async def complete(
    prompt: str,
    system_prompt: str = "",
    response_format: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Async completion function compatible with OpenAI format."""
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("UNISYNC_LLM_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    target_model = model or os.getenv("UNISYNC_LLM_MODEL", "gpt-4o-mini")

    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        logger.debug("No LLM API key provided; returning fallback JSON")
        if response_format and response_format.get("type") == "json_object":
            return json.dumps({"is_actionable": False, "summary": "", "items": [], "highlights": []})
        return ""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "temperature": kwargs.get("temperature", 0.1),
    }
    if response_format:
        payload["response_format"] = response_format

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            res.raise_for_status()
            data = res.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("UniSync LLM completion failed: %s", exc)
        if response_format and response_format.get("type") == "json_object":
            return json.dumps({"is_actionable": False, "summary": "", "items": [], "highlights": []})
        return ""
