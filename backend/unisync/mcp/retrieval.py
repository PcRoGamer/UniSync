"""Scoped, resumable reading of UniSync academic files.

No arbitrary filesystem access: only approved content roots, with symlinks
resolved before authorization. Every returned text page is scrubbed before
offsets are calculated, making continuation deterministic and complete for
the *scrubbed text representation*. Binary/visual content is flagged, not leaked.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from unisync.core.path_service import get_path_service
from unisync.mcp.egress import scrub_text

_ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".txt", ".md", ".vtt", ".csv", ".json",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".r",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sh", ".tex",
}


def _allowed_roots() -> tuple[Path, ...]:
    base = get_path_service().get_user_root().resolve()
    return tuple((base / rel).resolve() for rel in (
        "courses", "workspace/courses", "workspace/generated",
        "workspace/exports", "media_library", "cache/echo360_subtitles",
    ))


def authorized_file(file_path: str) -> Path:
    """Resolve path once, enforce approved roots, reject other files and symlink escapes."""
    base = get_path_service().get_user_root().resolve()
    requested = Path(file_path)
    candidates = [requested] if requested.is_absolute() else [
        base / requested, base / "workspace" / requested,
        base / "courses" / requested,
    ]
    for path in candidates:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) for root in _allowed_roots()):
            continue
        if not resolved.is_file() or resolved.suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        return resolved
    raise PermissionError("Requested file is missing or outside the approved UniSync academic directories.")


def _text_for_page(file: Path, page: int) -> tuple[str, int, bool, dict[str, Any]]:
    """Return one logical page, total pages, visual-content flag, metadata."""
    ext = file.suffix.lower()
    if ext == ".pdf":
        import pypdf
        pdf = pypdf.PdfReader(str(file))
        count = len(pdf.pages)
        if not 1 <= page <= count:
            raise ValueError(f"page must be 1..{count}")
        p = pdf.pages[page - 1]
        images = len(p.images)
        content = p.extract_text(extraction_mode="plain") or ""
        return f"--- Page {page} ---\n{content}", count, images > 0, {"images_on_page": images, "total_pages": count}
    if ext == ".pptx":
        import pptx
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        slides = pptx.Presentation(str(file)).slides
        count = len(slides)
        if not 1 <= page <= count:
            raise ValueError(f"slide must be 1..{count}")
        slide = slides[page - 1]
        chunks = []
        visuals = 0
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text.strip():
                chunks.append(shape.text.strip())
            if shape.has_table:
                for row in shape.table.rows:
                    chunks.append(" | ".join(cell.text for cell in row.cells))
            if shape.shape_type in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.CHART):
                visuals += 1
        return "--- Slide %d ---\n%s" % (page, "\n".join(chunks)), count, visuals > 0, {"images_or_charts_on_slide": visuals, "total_slides": count}
    if page != 1:
        raise ValueError("Non-PDF/PPTX files have one logical text unit; use offset to continue.")
    if ext == ".docx":
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = docx.Document(str(file))
        chunks = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, document)
                text = para.text
                if text:
                    style = para.style.name.lower() if para.style else ""
                    prefix = ("## " if "heading" in style else "")
                    chunks.append(prefix + text)
            elif child.tag == qn("w:tbl"):
                table = Table(child, document)
                for row in table.rows:
                    chunks.append(" | ".join(cell.text for cell in row.cells))
        visual_count = len(document.inline_shapes)
        return "\n\n".join(chunks), 1, visual_count > 0, {
            "inline_images": visual_count, "paragraphs_count": len(document.paragraphs),
            "tables_count": len(document.tables),
        }
    raw = file.read_text(encoding="utf-8", errors="replace")
    if ext in (".html", ".xml"):
        raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return raw, 1, False, {}


def _sha256(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_academic_file(
    file_path: str, *, max_chars: int = 8000, offset: int = 0, page: int = 1
) -> dict[str, Any]:
    """Retrieve all scrubbed text using (page, offset) continuation.

    Page means PDF page / PowerPoint slide (1-based), otherwise the whole text
    file or Word document. Offsets count Python Unicode characters in the
    scrubbed text for the given page. No tool output is allowed before scrubbing.
    """
    if not 128 <= max_chars <= 40000:
        raise ValueError("max_chars must be between 128 and 40000.")
    if offset < 0 or page < 1:
        raise ValueError("offset must be >=0 and page must be >=1.")
    file = authorized_file(file_path)
    text, total_pages, visuals, details = _text_for_page(file, page)
    clean = scrub_text(text)
    total = len(clean)
    if offset > total:
        raise ValueError(f"offset exceeds text length ({total}).")
    end = min(total, offset + max_chars)
    next_offset = end if end < total else None
    next_page = page + 1 if next_offset is None and page < total_pages else None
    has_more = next_offset is not None or next_page is not None
    base = get_path_service().get_user_root().resolve()
    return {
        "file": file.name, "file_path": file.relative_to(base).as_posix(),
        "file_type": file.suffix.lower().lstrip("."), "page": page,
        "total_pages": total_pages, "offset": offset, "returned_chars": end - offset,
        "page_text_chars": total, "truncated": has_more, "has_more": has_more,
        "next_page": next_page, "next_offset": next_offset,
        "content": clean[offset:end],
        "source_sha256": _sha256(file), "scrubber": "academic_boilerplate+credential_redaction",
        "visual_content_present": visuals,
        "text_only": True,
        "fidelity_note": (
            "Visual images/diagrams are not represented in this text. "
            "Consult the original academic file for non-text content."
            if visuals else "Text-layer extraction; original layout may differ."
        ),
        **details,
    }
