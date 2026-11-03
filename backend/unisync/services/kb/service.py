"""UniSync Knowledge Base Service.

Orchestrates document parsing, txtai hybrid vector indexing (FAISS + BM25),
and incremental persistence for university course materials, lecture transcripts,
and administrative communications.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from txtai import Embeddings

from unisync.services.kb.parsers import (
    DoclingParser,
    DocumentChunk,
    EmailNoticeParser,
    FastCourseDocParser,
    MarkdownTextParser,
    VttTranscriptParser,
)

logger = logging.getLogger("unisync.kb.service")


class KnowledgeBaseService:
    """Manages the semantic knowledge base index and retrieval."""

    def __init__(
        self,
        kb_dir: Path | str | None = None,
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        if kb_dir is None:
            # Default to data/kb in UniSync repository root
            base_dir = Path(__file__).resolve().parents[4]  # c:\Users\Admin\Desktop\UniSync
            self.kb_dir = base_dir / "data" / "kb"
        else:
            self.kb_dir = Path(kb_dir)

        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir = self.kb_dir / "index"
        self.manifest_path = self.kb_dir / "manifest.json"
        self.embedding_model = embedding_model

        # Document parsers
        self.fast_doc_parser = FastCourseDocParser()
        self.md_parser = MarkdownTextParser()
        self.docling_parser = DoclingParser()
        self.vtt_parser = VttTranscriptParser()
        self.email_parser = EmailNoticeParser()

        # Load or initialize txtai embeddings
        self._embeddings: Embeddings | None = None
        self._manifest: dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                data.setdefault("files", {})
                data.setdefault("hashes", {})
                return data
            except Exception as exc:
                logger.warning("Failed to load KB manifest, initializing empty: %s", exc)
        return {"updated_at": None, "total_chunks": 0, "files": {}, "hashes": {}}

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _save_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")

    def _get_embeddings(self) -> Embeddings:
        """Lazy load or initialize the txtai embeddings instance."""
        if self._embeddings is None:
            config_file = self.index_dir / "config.json"
            if not config_file.exists():
                config_file = self.index_dir / "config"

            if self.index_dir.exists() and config_file.exists():
                try:
                    logger.info("Loading existing Knowledge Base from %s", self.index_dir)
                    emb = Embeddings()
                    emb.load(str(self.index_dir))
                    self._embeddings = emb
                    return self._embeddings
                except Exception as exc:
                    logger.warning("Failed to load existing index, re-initializing: %s", exc)

            logger.info("Initializing new Knowledge Base with model %s", self.embedding_model)
            self._embeddings = Embeddings(
                {
                    "path": self.embedding_model,
                    "content": True,  # Stores full text and metadata in SQLite
                    "hybrid": True,   # Combines dense vector similarity with BM25 keywords
                }
            )
        return self._embeddings

    def search(
        self,
        query: str,
        course: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Perform semantic hybrid search across indexed course materials."""
        embeddings = self._get_embeddings()

        # Retrieve a broader candidate set for two-stage cross-encoder reranking
        fetch_limit = max(limit * 3, 15)
        safe_query = query.replace("'", "''")
        columns = (
            "id, text, course, file_name, file_path, content_type, "
            "section_title, timestamp_start, timestamp_end, score"
        )

        if course:
            safe_course = course.replace("'", "''")
            sql = (
                f"select {columns} from txtai where similar('{safe_query}') "
                f"and course like '%{safe_course}%' limit {fetch_limit}"
            )
        else:
            sql = f"select {columns} from txtai where similar('{safe_query}') limit {fetch_limit}"

        try:
            raw_results = embeddings.search(sql)
        except Exception as exc:
            logger.warning("SQL search failed, falling back to standard search: %s", exc)
            raw_results = embeddings.search(query, limit=fetch_limit)

        formatted: list[dict[str, Any]] = []
        for r in raw_results:
            formatted.append(
                {
                    "id": r.get("id"),
                    "score": round(float(r.get("score", 0.0)), 4),
                    "text": r.get("text", ""),
                    "file_name": r.get("file_name", ""),
                    "file_path": r.get("file_path", ""),
                    "course": r.get("course", ""),
                    "content_type": r.get("content_type", "document"),
                    "section_title": r.get("section_title"),
                    "timestamp_start": r.get("timestamp_start"),
                    "timestamp_end": r.get("timestamp_end"),
                }
            )

        # Stage 2: Cross-encoder reranking with FlashRank
        if formatted:
            try:
                from unisync.services.kb.reranker import rerank_passages
                formatted = rerank_passages(query=query, passages=formatted, top_k=limit)
            except Exception as _r_err:
                logger.debug("FlashRank reranking skipped: %s", _r_err)
                formatted = formatted[:limit]

        return formatted

    def index_all(
        self,
        courses_dir: Path | str | None = None,
        subtitles_dir: Path | str | None = None,
        emails_dir: Path | str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Index all course files, lecture subtitles, and university emails."""
        if not _index_lock.acquire(blocking=False):
            logger.info("KB reindex skipped — another reindex is already running")
            return {
                "status": "busy-skipped",
                "new_chunks_indexed": 0,
                "files_indexed": 0,
                "files_skipped": 0,
            }
        try:
            return self._index_all_inner(
                courses_dir=courses_dir,
                subtitles_dir=subtitles_dir,
                emails_dir=emails_dir,
                force=force,
            )
        finally:
            _index_lock.release()

    # Backwards-compatible alias for callers expecting scan_and_index
    scan_and_index = index_all

    def _index_all_inner(
        self,
        courses_dir: Path | str | None = None,
        subtitles_dir: Path | str | None = None,
        emails_dir: Path | str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Index all course files, lecture subtitles, and university emails."""
        base_dir = self.kb_dir.parent  # data/
        c_dir = Path(courses_dir) if courses_dir else (base_dir / "courses")
        s_dir = Path(subtitles_dir) if subtitles_dir else (base_dir / "cache" / "echo360_subtitles")
        e_dir = Path(emails_dir) if emails_dir else (base_dir / "workspace" / "emails")

        all_chunks: list[DocumentChunk] = []
        files_indexed_count = 0
        files_skipped_count = 0

        # 1. Parse Course Documents (PDF, DOCX, PPTX)
        if c_dir.exists():
            for f in c_dir.glob("**/*"):
                if not f.is_file() or f.suffix.lower() not in (".pdf", ".docx", ".pptx", ".txt", ".md"):
                    continue

                mtime = f.stat().st_mtime
                file_key = str(f.resolve())
                if not force and file_key in self._manifest["files"]:
                    if self._manifest["files"][file_key].get("mtime") == mtime:
                        files_skipped_count += 1
                        continue

                # Content-hash dedup: identical bytes under another name
                # (e.g. Canvas re-download "file-1.pdf") index only once.
                try:
                    content_hash = self._sha256_file(f)
                except Exception:
                    content_hash = ""
                if content_hash and not force:
                    first_seen = self._manifest["hashes"].get(content_hash)
                    if first_seen and first_seen != file_key:
                        self._manifest["files"][file_key] = {
                            "mtime": mtime,
                            "chunk_count": 0,
                            "type": "duplicate",
                            "duplicate_of": first_seen,
                        }
                        files_skipped_count += 1
                        continue

                course_name = f.parent.name
                # If nested in raw/canvas/<id>, try to find course folder name
                for p in f.parents:
                    if p.parent == c_dir:
                        course_name = p.name
                        break

                try:
                    if f.suffix.lower() in (".md", ".txt"):
                        chunks = self.md_parser.parse(f, course_name=course_name)
                        doc_type = "page" if "page" in f.parent.name.lower() or "page" in f.name.lower() else "document"
                    else:
                        chunks = self.fast_doc_parser.parse(f, course_name=course_name)
                        doc_type = "document"

                    if chunks:
                        all_chunks.extend(chunks)
                        files_indexed_count += 1
                        self._manifest["files"][file_key] = {
                            "mtime": mtime,
                            "chunk_count": len(chunks),
                            "type": doc_type,
                        }
                        if content_hash:
                            self._manifest["hashes"].setdefault(content_hash, file_key)
                    else:
                        # Parsed but produced nothing (e.g. scanned PDF) —
                        # record so it isn't re-parsed every reindex.
                        self._manifest["files"][file_key] = {
                            "mtime": mtime,
                            "chunk_count": 0,
                            "type": doc_type,
                        }
                        if content_hash:
                            self._manifest["hashes"].setdefault(content_hash, file_key)
                except Exception as exc:
                    logger.error("Error parsing document %s: %s", f.name, exc)
                    # Record the failure so a deterministically-broken file
                    # (e.g. docling OOM) isn't retried on every reindex.
                    # A later mtime change or force=True still retries it.
                    self._manifest["files"][file_key] = {
                        "mtime": mtime,
                        "chunk_count": 0,
                        "type": "failed",
                        "error": str(exc)[:300],
                    }

        # 2. Parse Echo360 Lecture Subtitles (.vtt)
        if s_dir.exists():
            section_map: dict[str, str] = {}
            try:
                from unisync.api.lectures import _load_libraries
                for lib in _load_libraries().values():
                    sec_id = lib.get("echo360_course_id")
                    kb = lib.get("kb_name") or lib.get("name")
                    if sec_id and kb:
                        section_map[sec_id] = kb
            except Exception:
                pass

            for f in s_dir.glob("*.vtt"):
                if not f.is_file():
                    continue

                mtime = f.stat().st_mtime
                file_key = str(f.resolve())
                if not force and file_key in self._manifest["files"]:
                    if self._manifest["files"][file_key].get("mtime") == mtime:
                        files_skipped_count += 1
                        continue

                course_name = "Lecture Recording"
                if "blaw" in f.name.lower():
                    course_name = "BLAW10001_2026_SM2"
                else:
                    for sec_id, cname in section_map.items():
                        if sec_id in f.name:
                            course_name = cname
                            break

                try:
                    chunks = self.vtt_parser.parse(f, course_name=course_name)
                    if chunks:
                        all_chunks.extend(chunks)
                        files_indexed_count += 1
                        self._manifest["files"][file_key] = {
                            "mtime": mtime,
                            "chunk_count": len(chunks),
                            "type": "lecture_transcript",
                        }
                except Exception as exc:
                    logger.error("Error parsing transcript %s: %s", f.name, exc)

        # 3. Parse Official Emails & Sanction Notices
        if e_dir.exists():
            for edir in e_dir.iterdir():
                if not edir.is_dir():
                    continue

                file_key = str(edir.resolve())
                b_file = edir / "body.txt"
                mtime = b_file.stat().st_mtime if b_file.exists() else 0
                if not force and file_key in self._manifest["files"]:
                    if self._manifest["files"][file_key].get("mtime") == mtime:
                        files_skipped_count += 1
                        continue

                try:
                    chunks = self.email_parser.parse(edir)
                    if chunks:
                        all_chunks.extend(chunks)
                        files_indexed_count += 1
                        self._manifest["files"][file_key] = {
                            "mtime": mtime,
                            "chunk_count": len(chunks),
                            "type": "email",
                        }
                except Exception as exc:
                    logger.error("Error parsing email notice %s: %s", edir.name, exc)

        # 4. Upsert/Index into txtai
        if all_chunks:
            embeddings = self._get_embeddings()

            def chunk_stream():
                for c in all_chunks:
                    yield {
                        "id": c.id,
                        "text": c.text,
                        "file_name": c.file_name,
                        "file_path": c.source_file,
                        "course": c.course,
                        "content_type": c.content_type,
                        "section_title": c.section_title,
                        "timestamp_start": c.timestamp_start,
                        "timestamp_end": c.timestamp_end,
                    }

            has_index = (self.index_dir / "config.json").exists() or (self.index_dir / "config").exists()
            if force or not has_index:
                embeddings.index(chunk_stream())
            else:
                embeddings.upsert(chunk_stream())

            embeddings.save(str(self.index_dir))
            logger.info("Saved updated index to %s (%d new chunks)", self.index_dir, len(all_chunks))
            # Enforce WAL mode on txtai SQLite databases to prevent cross-process locking
            try:
                import sqlite3
                for db_name in ("documents", "scoring.terms"):
                    db_path = self.index_dir / db_name
                    if db_path.exists():
                        with sqlite3.connect(str(db_path)) as con:
                            con.execute("PRAGMA journal_mode=WAL;")
            except Exception as _wal_err:
                logger.debug("Failed setting WAL mode on txtai index: %s", _wal_err)

        self._manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._manifest["total_chunks"] = self._get_embeddings().count()
        self._save_manifest()

        return {
            "new_chunks_indexed": len(all_chunks),
            "files_indexed": files_indexed_count,
            "files_skipped": files_skipped_count,
            "total_chunks_in_index": self._manifest["total_chunks"],
            "index_path": str(self.index_dir),
        }

    def get_stats(self) -> dict[str, Any]:
        """Return knowledge base statistics."""
        return {
            "total_chunks": self._manifest.get("total_chunks", 0),
            "indexed_files": len(self._manifest.get("files", {})),
            "updated_at": self._manifest.get("updated_at"),
            "index_path": str(self.index_dir),
        }


# Serializes full reindexes: txtai/FAISS writes are not thread-safe, and
# overlapping runs (two tabs pushing seconds apart) segfault the server.
_index_lock = threading.Lock()

# Global singleton instance
_kb_instance: KnowledgeBaseService | None = None


def get_kb_service() -> KnowledgeBaseService:
    """Returns the singleton KnowledgeBaseService instance."""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBaseService()
    return _kb_instance
