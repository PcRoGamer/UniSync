"""High-Performance Download Registry & Provenance Index for UniSync.

Provides instant O(1) resolution for AI agents, RAG ingestion pipelines,
and sync workers to resolve:
  - Local filepath -> Direct LMS/Portal download URL & course metadata
  - Direct URL / ID -> Local cached filepath on disk

Key Features:
  - O(1) Bidirectional indexing (by path, by filename, by URL, by ID)
  - In-memory cache with thread-safe locking
  - Debounced / batch-safe Markdown inventory generation (no I/O thrashing)
  - Typed `DownloadItem` representation
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from unisync.core.file_io import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadItem:
    """Represents a downloaded asset with its provenance and local path."""
    key: str
    platform: str
    item_id: str
    title: str
    direct_link: str
    local_filepath: str
    course_id: str = ""
    course_name: str = ""
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    downloaded_at: str = ""
    metadata: dict[str, Any] = None  # type: ignore

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def path(self) -> Path:
        return Path(self.local_filepath)

    def exists(self) -> bool:
        return self.path.exists()


class DownloadRegistry:
    """Thread-safe, multi-indexed provenance registry."""

    _instance: Optional[DownloadRegistry] = None
    _lock = threading.RLock()

    def __init__(self, data_path: Optional[Path] = None):
        self._data_path = data_path or self._resolve_manifest_path()
        self._items: dict[str, DownloadItem] = {}
        self._by_path: dict[str, str] = {}         # normalized path -> key
        self._by_filename: dict[str, list[str]] = {} # filename -> list of keys
        self._by_url: dict[str, str] = {}          # exact url -> key
        self._by_id: dict[str, str] = {}           # platform:id -> key
        self._updated_at: str = ""
        self._dirty: bool = False
        self._load()
        self._deduplicate_local_paths()
        self._reconcile_local_assets()

    @classmethod
    def get_instance(cls) -> DownloadRegistry:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _resolve_manifest_path() -> Path:
        # Keep the manifest in the platform-independent UniSync data root.
        # Import lazily to avoid coupling module initialization to PathService.
        from unisync.core.path_service import get_path_service

        return get_path_service().get_user_root() / "downloads_manifest.json"

    @staticmethod
    def _normalize_path(p: str | Path) -> str:
        try:
            return str(Path(p).resolve()).lower().replace("/", "\\")
        except Exception:
            return str(p).lower().replace("/", "\\")

    def _load(self) -> None:
        """Load manifest and build multi-index caches."""
        if not self._data_path.exists():
            return

        try:
            raw = json.loads(self._data_path.read_text(encoding="utf-8"))
            self._updated_at = raw.get("updated_at", "")
            items_raw = raw.get("items", {})

            with self._lock:
                self._items.clear()
                self._by_path.clear()
                self._by_filename.clear()
                self._by_url.clear()
                self._by_id.clear()

                for k, v in items_raw.items():
                    item = DownloadItem(
                        key=v.get("key", k),
                        platform=v.get("platform", ""),
                        item_id=str(v.get("item_id", "")),
                        title=v.get("title", ""),
                        direct_link=v.get("direct_link", ""),
                        local_filepath=v.get("local_filepath", ""),
                        course_id=str(v.get("course_id", "")),
                        course_name=v.get("course_name", ""),
                        size_bytes=v.get("size_bytes", 0),
                        content_type=v.get("content_type", "application/octet-stream"),
                        downloaded_at=v.get("downloaded_at", ""),
                        metadata=v.get("metadata", {}),
                    )
                    self._index_item(item)
            logger.info("DownloadRegistry indexed %d items from %s", len(self._items), self._data_path)
        except Exception as exc:
            logger.error("Failed to load DownloadRegistry: %s", exc)

    def _rebuild_indexes(self) -> None:
        """Rebuild secondary indexes after an inventory repair."""
        self._by_path.clear()
        self._by_filename.clear()
        self._by_url.clear()
        self._by_id.clear()
        for item in self._items.values():
            self._index_item(item)

    def _deduplicate_local_paths(self) -> None:
        """Keep one provenance record per local file, preferring the newest one."""
        with self._lock:
            original_count = len(self._items)
            unique_by_path: dict[str, DownloadItem] = {}
            without_path: list[DownloadItem] = []
            for item in self._items.values():
                if not item.local_filepath:
                    without_path.append(item)
                    continue
                unique_by_path[self._normalize_path(item.local_filepath)] = item

            deduplicated = without_path + list(unique_by_path.values())
            if len(deduplicated) == len(self._items):
                return

            self._items = {item.key: item for item in deduplicated}
            self._rebuild_indexes()
            self._dirty = True
            logger.info(
                "Removed %d duplicate inventory record(s) by local path",
                original_count - len(deduplicated),
            )

    def _reconcile_local_assets(self) -> None:
        """Index local sync outputs that predate the shared download manifest."""
        try:
            from unisync.core.path_service import get_path_service

            data_root = get_path_service().get_user_root()
            added = 0

            # Echo360 subtitle tracks are useful downloaded assets even when
            # the associated video stream is unavailable or still queued.
            from unisync.mcp.course_scope import known_courses, lecture_course
            catalog = known_courses()
            repaired = 0

            subtitles_dir = data_root / "cache" / "echo360_subtitles"
            for subtitle_path in subtitles_dir.glob("*.vtt") if subtitles_dir.exists() else []:
                stem = subtitle_path.stem
                owner = lecture_course(stem, catalog=catalog)
                library = catalog.get(owner, {})
                item_id = f"{stem}:subtitles"
                key = f"echo360:{item_id}"
                normalized_path = self._normalize_path(subtitle_path)
                if key in self._items:
                    item = self._items[key]
                    if owner and not item.course_name:
                        item.course_id = library["section_id"]
                        item.course_name = library["full_title"]
                        item.metadata["course_code"] = owner
                        item.metadata["ownership_method"] = "verified_echo360_section"
                        self._dirty = True
                        repaired += 1
                    continue
                if any(
                    self._normalize_path(item.local_filepath) == normalized_path
                    for item in self._items.values()
                    if item.local_filepath
                ):
                    continue
                self.register(
                    platform="echo360",
                    item_id=item_id,
                    title=f"{stem} — subtitles.vtt",
                    direct_link=(
                        f"https://echo360.net.au/media/download/{stem}/"
                        f"transcript.vtt?lessonId={stem}"
                    ),
                    local_filepath=subtitle_path,
                    course_id=str(library.get("section_id", "")),
                    course_name=str(library.get("full_title", "")),
                    size_bytes=subtitle_path.stat().st_size,
                    content_type="text/vtt",
                    metadata={"asset_type": "subtitle", "course_code": owner or "", "ownership_method": "verified_echo360_section" if owner else "unclassified"},
                    auto_flush=False,
                )
                added += 1

            # Also reconcile completed Echo360 media files written before the
            # registration hook was added to the background downloader.
            media_root = data_root / "media_library"
            if media_root.exists():
                for metadata_path in media_root.glob("*/metadata.json"):
                    try:
                        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                        media_path = metadata_path.parent / str(meta.get("media_file", ""))
                        source_id = str(meta.get("source_id", ""))
                        if (
                            meta.get("source") == "echo360"
                            and source_id
                            and media_path.exists()
                            and media_path.stat().st_size > 0
                            and f"echo360:{source_id}" not in self._items
                        ):
                            self.register(
                                platform="echo360",
                                item_id=source_id,
                                title=str(meta.get("title") or media_path.name),
                                direct_link=str(meta.get("source_url") or ""),
                                local_filepath=media_path,
                                course_id=str(meta.get("course_id", "")),
                                course_name=str(meta.get("course_name", "")),
                                size_bytes=media_path.stat().st_size,
                                content_type=str(meta.get("content_type") or "video/mp4"),
                                metadata={"asset_type": "recording"},
                                auto_flush=False,
                            )
                            added += 1
                    except (OSError, TypeError, ValueError):
                        continue

            if added or repaired:
                self.flush()
                logger.info("Reconciled %d assets and repaired %d subtitle owners", added, repaired)
        except (OSError, TypeError, ValueError):
            logger.debug("Local asset reconciliation skipped", exc_info=True)

    def _index_item(self, item: DownloadItem) -> None:
        key = item.key
        self._items[key] = item

        # Index by normalized path
        if item.local_filepath:
            norm_p = self._normalize_path(item.local_filepath)
            self._by_path[norm_p] = key
            fn = Path(item.local_filepath).name.lower()
            if fn:
                self._by_filename.setdefault(fn, []).append(key)

        # Index by title filename
        if item.title:
            fn_title = Path(item.title).name.lower()
            if fn_title and fn_title not in self._by_filename:
                self._by_filename.setdefault(fn_title, []).append(key)

        # Index by direct URL
        if item.direct_link:
            self._by_url[item.direct_link.strip()] = key
            # Also without query parameters
            clean_url = item.direct_link.split("?")[0].strip()
            if clean_url:
                self._by_url.setdefault(clean_url, key)

        # Index by platform + ID and ID alone
        if item.item_id:
            pid = f"{item.platform}:{item.item_id}".lower()
            self._by_id[pid] = key
            self._by_id.setdefault(item.item_id.lower(), key)

    def register(
        self,
        platform: str,
        item_id: str,
        title: str,
        direct_link: str,
        local_filepath: Path | str,
        course_id: str = "",
        course_name: str = "",
        size_bytes: int = 0,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, Any]] = None,
        auto_flush: bool = True,
    ) -> DownloadItem:
        """Register a download record. O(1) in-memory index + persistent write."""
        key = f"{platform}:{item_id}"
        path_str = str(Path(local_filepath).resolve()) if local_filepath else ""
        sz = size_bytes or (Path(path_str).stat().st_size if path_str and Path(path_str).exists() else 0)

        item = DownloadItem(
            key=key,
            platform=platform,
            item_id=str(item_id),
            title=title,
            direct_link=direct_link,
            local_filepath=path_str,
            course_id=course_id,
            course_name=course_name,
            size_bytes=sz,
            content_type=content_type,
            downloaded_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )

        with self._lock:
            self._index_item(item)
            self._dirty = True

        if auto_flush:
            self.flush()

        logger.info("Registered download: [%s] %s -> %s (Link: %s)", platform, title, path_str, direct_link)
        return item

    def flush(self) -> None:
        """Persist in-memory state to JSON atomically."""
        with self._lock:
            if not self._dirty and self._data_path.exists():
                return
            data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "items": {k: it.to_dict() for k, it in self._items.items()}
            }
            self._dirty = False

        atomic_write_json(self._data_path, data)

    def resolve(self, query: str | Path) -> Optional[DownloadItem]:
        """Instant O(1) resolution for AI agents and pipelines.

        Query can be:
          1. Absolute local path (e.g. C:/.../lecture.pdf)
          2. Relative path or bare filename (e.g. lecture.pdf)
          3. Direct download URL
          4. Item ID (e.g. 26804961 or echo360 video ID)
          5. Unique key (e.g. canvas:240718:26804961)
        """
        if not query:
            return None

        q_str = str(query).strip()
        with self._lock:
            # 1. Check direct key
            if q_str in self._items:
                return self._items[q_str]

            # 2. Check normalized path
            norm_p = self._normalize_path(q_str)
            if norm_p in self._by_path:
                return self._items[self._by_path[norm_p]]

            # 3. Check direct URL
            if q_str in self._by_url:
                return self._items[self._by_url[q_str]]
            clean_q_url = q_str.split("?")[0]
            if clean_q_url in self._by_url:
                return self._items[self._by_url[clean_q_url]]

            # 4. Check platform:ID or ID
            q_id = q_str.lower()
            if q_id in self._by_id:
                return self._items[self._by_id[q_id]]

            # 5. Check bare filename
            fn = Path(q_str).name.lower()
            if fn in self._by_filename:
                keys = self._by_filename[fn]
                if keys:
                    return self._items[keys[0]]

            # 6. Substring path search fallback (only for path-like queries)
            if "\\" in norm_p or "/" in norm_p:
                for p_key, it_key in self._by_path.items():
                    if norm_p in p_key:
                        return self._items[it_key]

        return None

    def list_all(self, platform: Optional[str] = None) -> list[DownloadItem]:
        with self._lock:
            items = list(self._items.values())
            if platform:
                p_lower = platform.lower()
                items = [it for it in items if it.platform.lower() == p_lower]
            return items

    def export_markdown(self, output_path: Optional[Path] = None) -> str:
        """Export comprehensive DOWNLOADS_INVENTORY.md table."""
        items = self.list_all()
        target = output_path or (self._data_path.parent.parent / "DOWNLOADS_INVENTORY.md")

        lines = [
            "# UniSync Downloads Inventory 📦",
            "",
            f"**Total Tracked Downloaded Assets & References**: `{len(items)}`",
            f"**Last Indexed**: `{datetime.now(timezone.utc).isoformat()}`",
            "",
            "This inventory provides the exact **direct download link** alongside the **local filesystem path** for every material, lecture subtitle, and reference tool in the project.",
            "",
            "---",
            "",
            "| Platform | Course / Context | Title / File | Size | Direct Link | Local Filepath | Downloaded At |",
            "|---|---|---|---|---|---|---|",
        ]

        # Sort by course name then title
        sorted_items = sorted(items, key=lambda x: (x.course_name or "", x.title or ""))

        for it in sorted_items:
            plat = it.platform.upper()
            cname = (it.course_name or it.course_id or "General").replace("|", "\\|")
            title = it.title.replace("|", "\\|")
            if it.size_bytes > 1024 * 1024:
                sz = f"{it.size_bytes / (1024 * 1024):.2f} MB"
            elif it.size_bytes > 0:
                sz = f"{it.size_bytes / 1024:.1f} KB"
            else:
                sz = "-"

            link = it.direct_link
            link_md = f"[Direct Link]({link})" if link.startswith("http") else f"`{link}`"
            path = it.local_filepath.replace("\\", "/")
            fn = Path(path).name or it.title
            file_md = f"[`{fn}`](file:///{path})"
            d_at = it.downloaded_at[:19].replace("T", " ") if it.downloaded_at else "-"

            lines.append(f"| **{plat}** | {cname} | {title} | {sz} | {link_md} | {file_md} | {d_at} |")

        md_text = "\n".join(lines) + "\n"
        atomic_write_text(target, md_text)
        logger.info("Exported markdown inventory to %s (%d items)", target, len(items))
        return md_text


# Module-level convenience functions
def get_registry() -> DownloadRegistry:
    return DownloadRegistry.get_instance()


get_download_registry = get_registry


def resolve_item(query: str | Path) -> Optional[DownloadItem]:
    """Instant lookup: given path, URL, filename, or ID -> returns DownloadItem."""
    return get_registry().resolve(query)


def register_download(
    platform: str,
    item_id: str,
    title: str,
    direct_link: str,
    local_filepath: Path | str,
    course_id: str = "",
    course_name: str = "",
    size_bytes: int = 0,
    content_type: str = "application/octet-stream",
    metadata: Optional[dict[str, Any]] = None,
    auto_flush: bool = True,
) -> DownloadItem:
    return get_registry().register(
        platform=platform,
        item_id=item_id,
        title=title,
        direct_link=direct_link,
        local_filepath=local_filepath,
        course_id=course_id,
        course_name=course_name,
        size_bytes=size_bytes,
        content_type=content_type,
        metadata=metadata,
        auto_flush=auto_flush,
    )


def flush_inventory() -> None:
    get_registry().flush()


def export_markdown_inventory(output_path: Optional[Path] = None) -> str:
    return get_registry().export_markdown(output_path)


# Compatibility aliases
def load_manifest() -> dict[str, Any]:
    reg = get_registry()
    return {
        "updated_at": reg._updated_at,
        "items": {k: it.to_dict() for k, it in reg._items.items()}
    }


def save_manifest(data: dict[str, Any]) -> None:
    get_registry().flush()


def generate_markdown_inventory(data: Optional[dict[str, Any]] = None, output_path: Optional[Path] = None) -> str:
    return get_registry().export_markdown(output_path)
