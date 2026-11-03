"""FlashRank cross-encoder reranking service for UniSync Knowledge Base.

Provides sub-10ms CPU cross-encoder reranking using ultra-lightweight ONNX models
(ms-marco-TinyBERT-L-2-v2, ~3.3MB) without heavy deep learning frameworks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("unisync.kb.reranker")

_RANKER_INSTANCE = None
_INIT_ATTEMPTED = False


def get_ranker():
    """Lazy initialize and return singleton FlashRank Ranker instance."""
    global _RANKER_INSTANCE, _INIT_ATTEMPTED
    if _RANKER_INSTANCE is not None:
        return _RANKER_INSTANCE

    if _INIT_ATTEMPTED and _RANKER_INSTANCE is None:
        return None

    _INIT_ATTEMPTED = True
    try:
        from flashrank import Ranker
        from unisync.core.path_service import get_path_service

        cache_dir = get_path_service().get_user_root() / "cache" / "flashrank"
        cache_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Initializing FlashRank Ranker (ms-marco-TinyBERT-L-2-v2)...")
        _RANKER_INSTANCE = Ranker(
            model_name="ms-marco-TinyBERT-L-2-v2",
            cache_dir=str(cache_dir),
        )
        return _RANKER_INSTANCE
    except Exception as exc:
        logger.warning("FlashRank unavailable, falling back to vector/BM25 rank: %s", exc)
        return None


def rerank_passages(
    query: str,
    passages: list[dict[str, Any]],
    top_k: int | None = None,
    text_key: str = "text",
) -> list[dict[str, Any]]:
    """Rerank candidate passages using FlashRank cross-encoder.

    Args:
        query: The user search query or question.
        passages: List of candidate passage dictionaries.
        top_k: Number of top results to return (default: return all reranked).
        text_key: Key in passage dict containing the text content to score ('text' or 'excerpt').

    Returns:
        Sorted list of passages with updated 'rerank_score' and 'score'.
    """
    if not passages or not query.strip():
        return passages[:top_k] if top_k else passages

    ranker = get_ranker()
    if ranker is None:
        return passages[:top_k] if top_k else passages

    try:
        from flashrank import RerankRequest

        formatted_passages = []
        for idx, item in enumerate(passages):
            raw_text = item.get(text_key) or item.get("text") or item.get("excerpt") or ""
            formatted_passages.append({
                "id": str(idx),
                "text": str(raw_text),
                "meta": item,
            })

        req = RerankRequest(query=query, passages=formatted_passages)
        ranked = ranker.rerank(req)

        reranked_results: list[dict[str, Any]] = []
        for r in ranked:
            original_item = dict(r["meta"])
            original_item["rerank_score"] = float(round(r["score"], 4))
            # Also update primary score for consumers
            original_item["score"] = float(round(r["score"], 4))
            reranked_results.append(original_item)

        if top_k is not None:
            return reranked_results[:top_k]
        return reranked_results

    except Exception as exc:
        logger.debug("FlashRank reranking skipped due to error: %s", exc)
        return passages[:top_k] if top_k else passages
