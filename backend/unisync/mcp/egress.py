"""Single MCP egress gate: academic scrubber plus credential redaction.

Every tool registered through the UniSync FastMCP instance is wrapped by this
gate. Neither tools/list metadata nor tool responses are ever sent unsanitized
by the wrapper. JSON response structures remain intact.
"""
from __future__ import annotations

import functools
import inspect
import json
import re
from typing import Any, Callable

from unisync.core.text_cleaner import filter_academic_boilerplate

_SECRET_PATTERNS = (
    # API and access tokens.
    (re.compile(r"(?i)\b(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_|glpat-|xox[baprs]-)[A-Za-z0-9_-]{12,}"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED_SECRET]"),
    (re.compile(r"(?im)(\b(?:api[_-]?key|password|secret|client_secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*['\"]?)[^\s,'\";}{]+"), r"\1[REDACTED_SECRET]"),
    (re.compile(r"(?im)(\b(?:Cookie|Set-Cookie|Authorization):\s*)[^\r\n]+"), r"\1[REDACTED_SECRET]"),
)


def redact_secrets(value: str) -> str:
    """Redact credentials without changing already-scrubbed pagination whitespace."""
    try:
        for pattern, replacement in _SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    except Exception:
        return "[CONTENT_SUPPRESSED_SCRUBBER_ERROR]"


def scrub_text(value: str) -> str:
    """Scrub academic boilerplate and common credential patterns, fail closed."""
    try:
        return redact_secrets(filter_academic_boilerplate(value))
    except Exception:
        return "[CONTENT_SUPPRESSED_SCRUBBER_ERROR]"


def _scrub_structure(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [_scrub_structure(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_structure(item) for item in value]
    if isinstance(value, dict):
        # Retrieval paginates AFTER scrubbing the entire document. Running the
        # academic filter again on an individual chunk strips boundary whitespace.
        pre_scrubbed = value.get("scrubber") == "academic_boilerplate+credential_redaction"
        return {
            key: (redact_secrets(item)
                  if pre_scrubbed and key in ("content", "transcript") and isinstance(item, str)
                  else _scrub_structure(item))
            for key, item in value.items()
        }
    return value


def scrub_mcp_response(raw: Any) -> Any:
    """Transform every MCP tool result. Preserve JSON types and stable schemas."""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return scrub_text(raw)
        if isinstance(payload, (dict, list)):
            return json.dumps(_scrub_structure(payload), ensure_ascii=False, indent=2)
        return scrub_text(raw)
    return _scrub_structure(raw)


def install_egress_guard(fastmcp: Any) -> None:
    """Wrap registration, so all current/future @mcp.tool() calls use the guard."""
    native_tool = fastmcp.tool

    @functools.wraps(native_tool)
    def guarded_tool(*decorator_args: Any, **decorator_kwargs: Any) -> Callable:
        decorate = native_tool(*decorator_args, **decorator_kwargs)

        def register(function: Callable) -> Any:
            if inspect.iscoroutinefunction(function):
                @functools.wraps(function)
                async def wrapped(*args: Any, **kwargs: Any) -> Any:
                    return scrub_mcp_response(await function(*args, **kwargs))
            else:
                @functools.wraps(function)
                def wrapped(*args: Any, **kwargs: Any) -> Any:
                    return scrub_mcp_response(function(*args, **kwargs))
            return decorate(wrapped)

        return register

    fastmcp.tool = guarded_tool
