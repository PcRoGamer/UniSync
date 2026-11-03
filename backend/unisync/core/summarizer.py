"""UniSync Summarize Agent."""

from typing import Any, Optional


class NotebookSummarizeAgent:
    """Summarizes text into structured study notes."""
    async def summarize(
        self,
        title: str,
        record_type: str = "lecture",
        user_query: str = "",
        output: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        if not output:
            return "No content to summarize."
        return output[:1600].strip()
