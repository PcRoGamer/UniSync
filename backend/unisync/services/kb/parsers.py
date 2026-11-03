"""Parsers for UniSync Knowledge Base.

Extracts structured, section-aware Markdown chunks from:
1. Plain text and markdown files (instant, zero overhead).
2. Course slides and documents (PDF via pypdf/Docling, DOCX via python-docx, PPTX via python-pptx).
3. Echo360 lecture transcripts (WebVTT) with precise timestamp windows.
4. Official university emails and sanction notices.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unisync.core.text_cleaner import (
    filter_academic_boilerplate,
    is_academic_boilerplate_section,
)

logger = logging.getLogger("unisync.kb.parsers")


@dataclass
class DocumentChunk:
    """A semantic chunk of text extracted from a course material."""
    id: str
    text: str
    source_file: str
    file_name: str
    course: str
    content_type: str  # "document", "lecture_transcript", "email", "page"
    section_title: str | None = None
    timestamp_start: str | None = None
    timestamp_end: str | None = None
    metadata: dict[str, Any] | None = None


def chunk_markdown_by_headings(md_text: str, max_chunk_chars: int = 1500) -> list[tuple[str, str]]:
    """Split markdown text into (heading, chunk_text) tuples preserving context and filtering boilerplate."""
    lines = md_text.splitlines()
    sections: list[tuple[str, str]] = []
    current_heading = "General"
    current_lines: list[str] = []

    def _push_section(heading: str, chunk_lines: list[str]) -> None:
        raw_body = "\n".join(chunk_lines).strip()
        if not raw_body:
            return
        # Skip sections whose heading or content is academic integrity / AI declaration boilerplate
        if is_academic_boilerplate_section(heading, raw_body):
            return
        cleaned = filter_academic_boilerplate(raw_body)
        if cleaned and len(cleaned) >= 15:
            sections.append((heading, cleaned))

    for line in lines:
        if re.match(r"^#{1,4}\s+", line):
            # If current buffer has text, push it
            if current_lines:
                _push_section(current_heading, current_lines)
                current_lines = []
            current_heading = line.lstrip("#").strip()
            current_lines.append(line)
        else:
            current_lines.append(line)

            # Split exceptionally long sections
            if sum(len(l) for l in current_lines) > max_chunk_chars:
                _push_section(current_heading, current_lines)
                current_lines = []

    if current_lines:
        _push_section(current_heading, current_lines)

    # If no headings were found or document is tiny, return cleaned body
    if not sections and md_text.strip():
        cleaned_all = filter_academic_boilerplate(md_text.strip())
        if cleaned_all and len(cleaned_all) >= 15:
            sections.append(("Content", cleaned_all))

    return sections


class MarkdownTextParser:
    """Parses plain Markdown and text files directly and cleanly."""

    def parse(self, filepath: Path, course_name: str = "") -> list[DocumentChunk]:
        try:
            md_text = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to read text file %s: %s", filepath.name, exc)
            return []

        if not md_text.strip():
            return []

        sections = chunk_markdown_by_headings(md_text)
        chunks: list[DocumentChunk] = []
        is_page = "page" in filepath.parent.name.lower() or "page" in filepath.name.lower()

        for idx, (heading, body) in enumerate(sections):
            if len(body.strip()) < 15:
                continue
            chunk_id = f"{filepath.stem}_{idx}"
            formatted_text = f"## [{course_name}] {filepath.name} — {heading}\n\n{body}"
            chunks.append(
                DocumentChunk(
                    id=chunk_id,
                    text=formatted_text,
                    source_file=str(filepath),
                    file_name=filepath.name,
                    course=course_name or filepath.parent.name,
                    content_type="page" if is_page else "document",
                    section_title=heading,
                    metadata={"char_count": len(body), "section_index": idx},
                )
            )

        return chunks


class DoclingParser:
    """Parses PDF, Word (docx), and PowerPoint (pptx) using Docling with CPU-safe options."""

    def __init__(self) -> None:
        self._converter = None

    def _get_converter(self):
        if self._converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions

            # Disable OCR on CPU to prevent ONNX runtime multi-thread hanging
            pdf_options = PdfPipelineOptions(do_ocr=False)
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
                }
            )
        return self._converter

    def parse(self, filepath: Path, course_name: str = "") -> list[DocumentChunk]:
        """Convert document to clean Markdown sections using Docling."""
        converter = self._get_converter()
        try:
            res = converter.convert(filepath)
            md_text = res.document.export_to_markdown()
        except Exception as exc:
            logger.warning("Docling failed on %s: %s", filepath.name, exc)
            return []

        sections = chunk_markdown_by_headings(md_text)
        chunks: list[DocumentChunk] = []

        for idx, (heading, body) in enumerate(sections):
            if len(body.strip()) < 20:
                continue
            chunk_id = f"{filepath.stem}_{idx}"
            formatted_text = f"## [{course_name}] {filepath.name} — {heading}\n\n{body}"
            chunks.append(
                DocumentChunk(
                    id=chunk_id,
                    text=formatted_text,
                    source_file=str(filepath),
                    file_name=filepath.name,
                    course=course_name or filepath.parent.name,
                    content_type="document",
                    section_title=heading,
                    metadata={"char_count": len(body), "section_index": idx},
                )
            )

        return chunks


class FastCourseDocParser:
    """High-speed parser for PDF, DOCX, and PPTX with intelligent fallback to Docling.
    
    Provides sub-second extraction for digital PDFs, Word documents, and slide decks,
    ensuring zero lag and zero CPU thread locking during bulk course syncs.
    """

    def __init__(self) -> None:
        self.docling_parser = DoclingParser()

    def parse(self, filepath: Path, course_name: str = "") -> list[DocumentChunk]:
        suffix = filepath.suffix.lower()

        if suffix == ".pdf":
            return self._parse_pdf(filepath, course_name)
        elif suffix == ".docx":
            return self._parse_docx(filepath, course_name)
        elif suffix == ".pptx":
            return self._parse_pptx(filepath, course_name)
        else:
            return self.docling_parser.parse(filepath, course_name)

    def _parse_pdf(self, filepath: Path, course_name: str) -> list[DocumentChunk]:
        """Extract digital text and section chunks from PDF via pypdf, falling back to Docling."""
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(filepath))
            pages_text: list[tuple[int, str]] = []
            total_chars = 0

            for i, page in enumerate(reader.pages):
                text = (page.extract_text() or "").strip()
                if text:
                    pages_text.append((i + 1, text))
                    total_chars += len(text)

            # If pypdf successfully extracted textual content
            if total_chars > 50:
                chunks: list[DocumentChunk] = []
                # Group 1-2 pages per chunk (~1000-2000 chars)
                current_pages: list[str] = []
                first_pnum = 1
                curr_char_count = 0

                for pnum, ptext in pages_text:
                    if not current_pages:
                        first_pnum = pnum
                    current_pages.append(f"### [Page {pnum}]\n{ptext}")
                    curr_char_count += len(ptext)

                    if curr_char_count >= 1200 or pnum == pages_text[-1][0]:
                        body = "\n\n".join(current_pages)
                        title = f"Pages {first_pnum}-{pnum}" if first_pnum != pnum else f"Page {first_pnum}"
                        chunk_id = f"{filepath.stem}_p{first_pnum}_{pnum}"
                        formatted_text = f"## [{course_name}] {filepath.name} — {title}\n\n{body}"
                        chunks.append(
                            DocumentChunk(
                                id=chunk_id,
                                text=formatted_text,
                                source_file=str(filepath),
                                file_name=filepath.name,
                                course=course_name or filepath.parent.name,
                                content_type="document",
                                section_title=title,
                                metadata={"pages": f"{first_pnum}-{pnum}", "char_count": len(body)},
                            )
                        )
                        current_pages = []
                        curr_char_count = 0

                return chunks
        except Exception as exc:
            logger.debug("pypdf extraction error on %s: %s", filepath.name, exc)

        # Fallback to Docling
        return self.docling_parser.parse(filepath, course_name)

    def _parse_docx(self, filepath: Path, course_name: str) -> list[DocumentChunk]:
        """Extract formatted headings and text from DOCX files."""
        try:
            import docx
            doc = docx.Document(str(filepath))
            md_lines: list[str] = []

            for p in doc.paragraphs:
                text = p.text.strip()
                if not text:
                    continue
                style_name = p.style.name.lower() if p.style else ""
                if "heading 1" in style_name:
                    md_lines.append(f"# {text}")
                elif "heading 2" in style_name:
                    md_lines.append(f"## {text}")
                elif "heading 3" in style_name:
                    md_lines.append(f"### {text}")
                elif "title" in style_name:
                    md_lines.append(f"# {text}")
                else:
                    md_lines.append(text)

            combined_md = "\n\n".join(md_lines)
            if combined_md:
                sections = chunk_markdown_by_headings(combined_md)
                chunks: list[DocumentChunk] = []
                for idx, (heading, body) in enumerate(sections):
                    if len(body.strip()) < 15:
                        continue
                    chunks.append(
                        DocumentChunk(
                            id=f"{filepath.stem}_{idx}",
                            text=f"## [{course_name}] {filepath.name} — {heading}\n\n{body}",
                            source_file=str(filepath),
                            file_name=filepath.name,
                            course=course_name or filepath.parent.name,
                            content_type="document",
                            section_title=heading,
                            metadata={"char_count": len(body), "section_index": idx},
                        )
                    )
                return chunks
        except Exception as exc:
            logger.debug("docx extraction error on %s: %s", filepath.name, exc)

        return self.docling_parser.parse(filepath, course_name)

    def _parse_pptx(self, filepath: Path, course_name: str) -> list[DocumentChunk]:
        """Extract slide-by-slide titles, text, and bullet points from PowerPoint decks."""
        try:
            import pptx
            prs = pptx.Presentation(str(filepath))
            chunks: list[DocumentChunk] = []

            for slide_num, slide in enumerate(prs.slides, start=1):
                slide_texts: list[str] = []
                slide_title = f"Slide {slide_num}"

                for shape in slide.shapes:
                    if shape.has_text_frame:
                        txt = shape.text.strip()
                        if txt:
                            if shape == slide.shapes.title:
                                slide_title = txt
                            else:
                                slide_texts.append(txt)

                body = "\n".join(slide_texts).strip()
                if not body and slide_title == f"Slide {slide_num}":
                    continue

                formatted_text = f"## [{course_name}] {filepath.name} — Slide {slide_num}: {slide_title}\n\n{body}"
                chunks.append(
                    DocumentChunk(
                        id=f"{filepath.stem}_slide_{slide_num}",
                        text=formatted_text,
                        source_file=str(filepath),
                        file_name=filepath.name,
                        course=course_name or filepath.parent.name,
                        content_type="document",
                        section_title=f"Slide {slide_num}: {slide_title}",
                        metadata={"slide_number": slide_num, "title": slide_title},
                    )
                )

            if chunks:
                return chunks
        except Exception as exc:
            logger.debug("pptx extraction error on %s: %s", filepath.name, exc)

        return self.docling_parser.parse(filepath, course_name)


class VttTranscriptParser:
    """Parses Echo360 WebVTT subtitle files into timestamped lecture windows."""

    def parse(self, filepath: Path, course_name: str = "Lecture Recording") -> list[DocumentChunk]:
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to read VTT file %s: %s", filepath, exc)
            return []

        lines = content.splitlines()
        cue_pattern = re.compile(
            r"(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
        )

        cues: list[tuple[str, str, str]] = []  # (start, end, text)
        current_start = ""
        current_end = ""
        current_text_lines: list[str] = []

        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("WEBVTT") or line_str.startswith("NOTE"):
                continue

            match = cue_pattern.search(line_str)
            if match:
                if current_text_lines:
                    text_combined = " ".join(current_text_lines).strip()
                    if text_combined:
                        cues.append((current_start, current_end, text_combined))
                    current_text_lines = []
                current_start = match.group(1)[:8]
                current_end = match.group(2)[:8]
            else:
                # Clean html formatting e.g. <v Speaker>
                cleaned = re.sub(r"<[^>]+>", "", line_str)
                if cleaned:
                    current_text_lines.append(cleaned)

        if current_text_lines:
            cues.append((current_start, current_end, " ".join(current_text_lines).strip()))

        # Group cues into ~2–3 minute windows (~150-250 words)
        chunks: list[DocumentChunk] = []
        window_start = ""
        window_end = ""
        window_text: list[str] = []

        for idx, (c_start, c_end, c_text) in enumerate(cues):
            if not window_start:
                window_start = c_start
            window_end = c_end
            window_text.append(c_text)

            # Flush roughly every 150 words or at end
            words = sum(len(t.split()) for t in window_text)
            if words >= 150 or idx == len(cues) - 1:
                body = " ".join(window_text).strip()
                if len(body) > 30:
                    chunk_id = f"{filepath.stem}_{len(chunks)}"
                    header = f"### [{course_name}] Lecture Transcript [{window_start} - {window_end}]"
                    formatted_text = f"{header}\n\n{body}"
                    chunks.append(
                        DocumentChunk(
                            id=chunk_id,
                            text=formatted_text,
                            source_file=str(filepath),
                            file_name=filepath.name,
                            course=course_name,
                            content_type="lecture_transcript",
                            section_title=f"Timestamp [{window_start} - {window_end}]",
                            timestamp_start=window_start,
                            timestamp_end=window_end,
                            metadata={"window_index": len(chunks)},
                        )
                    )
                window_start = ""
                window_text = []

        return chunks


class EmailNoticeParser:
    """Parses email bodies and metadata JSON into searchable notices."""

    def parse(self, email_dir: Path) -> list[DocumentChunk]:
        body_file = email_dir / "body.txt"
        meta_file = email_dir / "metadata.json"

        if not body_file.exists():
            return []

        body_text = body_file.read_text(encoding="utf-8", errors="replace").strip()
        if len(body_text) < 15:
            return []

        subject = email_dir.name
        sender = "University"
        date_str = ""

        if meta_file.exists():
            try:
                raw_meta = meta_file.read_text(encoding="utf-8", errors="replace")
                meta = json.loads(raw_meta)
                if isinstance(meta, dict):
                    subject = meta.get("subject") or subject
                    sender = meta.get("from") or sender
                    date_str = meta.get("date") or ""
            except Exception:
                pass

        header = f"### [Official Email Notice] {subject}\n**From:** {sender} | **Date:** {date_str[:10]}"
        formatted_text = f"{header}\n\n{body_text}"

        course = "University Notice"
        combined_lower = (subject + " " + body_text).lower()
        for code in ("BLAW10001", "COMP10001", "INFO10003", "CCDP10003"):
            if code.lower() in combined_lower:
                course = f"{code}_2026_SM2"
                break

        return [
            DocumentChunk(
                id=f"email_{email_dir.name}",
                text=formatted_text,
                source_file=str(body_file),
                file_name=subject,
                course=course,
                content_type="email",
                section_title=subject,
                metadata={"sender": sender, "date": date_str},
            )
        ]
