"""Course file gatherer and preprocessor for Google NotebookLM.

Collects and prepares course materials (Canvas documents, pages, Echo360
transcripts, EdStem lessons/files, and consolidated forum digests) for
uploading to Google NotebookLM.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from unisync.core.text_cleaner import filter_academic_boilerplate, is_boilerplate_page

logger = logging.getLogger(__name__)

SUPPORTED_DOC_EXTS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".txt",
    ".md",
    ".py",
    ".c",
    ".cpp",
    ".java",
    ".ipynb",
    ".csv",
}

DEFAULT_MAX_SOURCES = 290  # Pro tier allows 300, leaving headroom


@dataclasses.dataclass
class CourseFile:
    """Represents a course file prepared for NotebookLM upload."""
    path: Path
    rel_path: str
    title: str
    kind: str  # "document", "page", "transcript", "lesson", "forum_digest"
    size_bytes: int
    file_hash: str
    is_generated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rel_path": self.rel_path,
            "title": self.title,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "file_hash": self.file_hash,
            "is_generated": self.is_generated,
        }


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def sanitize_title(title: str, max_len: int = 150) -> str:
    """Sanitize title for NotebookLM source naming."""
    clean = re.sub(r'[\r\n\t]+', ' ', title).strip()
    clean = re.sub(r'[\\/:*?"<>|]+', ' - ', clean)
    clean = re.sub(r'\s{2,}', ' ', clean).strip(' -')
    return clean[:max_len] if clean else "Untitled Document"


SEMESTER_START = datetime.date(2026, 7, 27)

INFO_TOPICS = {
    1: "Subject Intro & Interaction Design",
    2: "About The Project & Teams",
    3: "Understanding Computers & Users",
    4: "Ethics & Accessibility",
    5: "Design Thinking",
    6: "Interaction Design Methods",
    7: "Interaction Design Practice",
}

CCDP_TOPICS = {
    1: "Technology, Demographics & Games",
    2: "Game Mechanics & Player Agency",
    3: "Narrative & World-Building",
    4: "Games That Design Themselves",
    5: "Personal Project Briefing",
    6: "Game Evaluation & Scoring",
    7: "Conceptual Game Design",
}


def clean_lesson_title(filename_stem: str) -> str:
    """Clean EdStem lesson filename into human-readable title.

    Transforms '36892-lessons-0024-readability' into 'EdStem Lesson 24 - Readability'.
    """
    m = re.match(r"^\d+[\s\-_]*lessons?[\s\-_]*0*(\d+)?[\s\-_]*(.*)$", filename_stem, re.IGNORECASE)
    if m:
        num = m.group(1)
        rest = m.group(2)
        clean_rest = rest.replace("-", " ").title()
        clean_rest = re.sub(r"\bCsc\b", "CSC", clean_rest)
        clean_rest = re.sub(r"\b1\s*Points?\b", "(1 pt)", clean_rest, flags=re.IGNORECASE)
        clean_rest = re.sub(r"\b(\d+)\s*Points?\b", r"(\1 pts)", clean_rest, flags=re.IGNORECASE)
        if num:
            return sanitize_title(f"EdStem Lesson {int(num):02d} - {clean_rest.strip()}")
        return sanitize_title(f"EdStem Lesson - {clean_rest.strip()}")
    return sanitize_title(f"EdStem Lesson - {filename_stem.replace('-', ' ').title()}")


def format_lecture_title(course_code: str, raw_title: str, vtt_name: str) -> str:
    """Format Echo360 lecture title with semester week, date, and topic."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", vtt_name)
    if m:
        try:
            dt = datetime.date.fromisoformat(m.group(1))
            diff = (dt - SEMESTER_START).days
            week = max(1, (diff // 7) + 1)
            date_str = dt.strftime("%d %b")
            if course_code == "INFO10003":
                topic = INFO_TOPICS.get(week, raw_title)
                return sanitize_title(f"Lecture Week {week:02d} ({date_str}) — {topic}")
            elif course_code == "CCDP10003":
                topic = CCDP_TOPICS.get(week, raw_title)
                return sanitize_title(f"Lecture Week {week:02d} ({date_str}) — {topic}")
            elif course_code == "COMP10001":
                if any(room in raw_title for room in ["TH B01", "TH GM15", "TU 122", "TU B101", "WE GM15", "WE 122"]):
                    return sanitize_title(f"Lecture Week {week:02d} ({date_str}) — {raw_title}")
                elif "mid-semester test" in raw_title.lower():
                    speaker = "Simon" if "33cca198" in vtt_name else "Geela" if "b885d35a" in vtt_name else ""
                    suffix = f" ({speaker})" if speaker else ""
                    return sanitize_title(f"Lecture Week {week:02d} ({date_str}) — Mid-Semester Test Review{suffix}")
                elif raw_title.startswith("W") and ("L1" in raw_title or "L2" in raw_title or "L3" in raw_title):
                    return sanitize_title(f"Lecture ({date_str}) — {raw_title}")
        except Exception:
            pass

    if course_code == "BLAW10001":
        if "echo360 video" in raw_title.lower() or "embedded content" in raw_title.lower():
            h = hashlib.md5(vtt_name.encode()).hexdigest()[:6]
            return sanitize_title(f"BLAW Lecture Video Clip [{h}]")

    return sanitize_title(f"Transcript — {raw_title}")


class CourseFileGatherer:
    """Gathers and prepares files across Canvas, Echo360, and EdStem for a subject."""

    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            self.project_root = Path(__file__).resolve().parents[4]
        else:
            self.project_root = Path(project_root)

        self.data_dir = self.project_root / "data"
        self.libraries_file = self.data_dir / "libraries" / "libraries.json"
        self.manifest_file = self.data_dir / "downloads_manifest.json"
        self.cache_dir = self.data_dir / "cache"
        self.transcripts_cache_dir = self.cache_dir / "notebooklm_transcripts"
        self.digests_cache_dir = self.cache_dir / "notebooklm_digests"
        self.consolidated_cache_dir = self.cache_dir / "notebooklm_consolidated"
        self.lessons_cache_dir = self.cache_dir / "notebooklm_lessons"
        self.pages_cache_dir = self.cache_dir / "notebooklm_pages"

    def get_enrolled_academic_courses(self) -> dict[str, dict[str, Any]]:
        """Return mapping of academic course codes to library metadata.

        Extracts enrolled university subjects like BLAW10001, COMP10001,
        INFO10003, CCDP10003 from libraries.json.
        """
        if not self.libraries_file.exists():
            return {}

        try:
            with open(self.libraries_file, "r", encoding="utf-8") as f:
                lib_data = json.load(f)
        except Exception as exc:
            logger.error("Failed to load libraries.json: %s", exc)
            return {}

        courses: dict[str, dict[str, Any]] = {}
        for cid, info in lib_data.items():
            name = info.get("name", "")
            kb_name = info.get("kb_name", "")
            # Look for 4 letters followed by 5 digits (e.g. BLAW10001, COMP10001)
            match = re.search(r"\b([A-Z]{4}\d{5})(?=[^A-Za-z0-9]|$)", name) or re.search(r"\b([A-Z]{4}\d{5})(?=[^A-Za-z0-9]|$)", kb_name)
            if match:
                code = match.group(1).upper()
                # Skip non-subject orientation/mentorship programs if needed
                if code.startswith("MPMP"):
                    continue
                # Clean up course name
                display_name = re.sub(r"\([^\)]*\)", "", name).strip()
                if not display_name:
                    display_name = code
                courses[code] = {
                    "course_code": code,
                    "course_name": display_name,
                    "canvas_course_id": info.get("canvas_course_id") or cid,
                    "echo360_course_id": info.get("echo360_course_id", ""),
                    "kb_name": kb_name or f"{code}_2026_SM2",
                    "full_library_name": name,
                }
        return courses

    def gather_all_sources(self, course_code: str, max_sources: int = DEFAULT_MAX_SOURCES) -> list[CourseFile]:
        """Gather, format, and prioritize all sources for a specific course code."""
        code = course_code.upper()
        courses = self.get_enrolled_academic_courses()
        course_info = courses.get(code)
        if not course_info:
            logger.warning("Course %s not found in registered academic courses", code)
            return []

        gathered: list[CourseFile] = []
        seen_hashes: set[str] = set()

        def add_file_if_valid(cf: Optional[CourseFile]):
            if cf is None:
                return
            if cf.file_hash in seen_hashes:
                return
            seen_hashes.add(cf.file_hash)
            gathered.append(cf)

        # 1. Consolidated High-Value Synthesis Documents:
        #    a) Comprehensive Course Overview & Syllabus
        add_file_if_valid(self._consolidate_course_overview(course_info))

        #    b) Consolidated Assignments & Assessments Guide (replaces fragmented stubs)
        add_file_if_valid(self._consolidate_assignments(course_info))

        #    c) Consolidated Announcements & Updates Digest
        add_file_if_valid(self._consolidate_announcements(course_info))

        #    d) Consolidated Canvas Discussions Digest (e.g. BLAW)
        add_file_if_valid(self._consolidate_discussions(course_info))

        # 2. Canvas Downloaded Documents (PDFs, DOCXs, PPTXs, code) - hash-deduplicated
        for cf in self._gather_canvas_download_documents(course_info):
            add_file_if_valid(cf)

        # 3. Canvas Rendered Markdown Pages (only genuine non-consolidated topic pages)
        for cf in self._gather_canvas_pages(course_info):
            add_file_if_valid(cf)

        # 4. Echo360 Lecture Transcripts (Formatted Markdown with Week/Date/Topic)
        for cf in self._gather_echo360_transcripts(course_info):
            add_file_if_valid(cf)

        # 5. EdStem Lessons and Course Files (e.g. COMP10001) - clean lesson titles
        for cf in self._gather_edstem_materials(course_info):
            add_file_if_valid(cf)

        # 6. EdStem Forum Threads -> Consolidated Weekly/Batch Digests
        for cf in self._generate_edstem_forum_digests(course_info):
            add_file_if_valid(cf)

        # Sort priority:
        # 1. Consolidated Guides & Documents (Overview, Assignments, Slides, PDFs)
        # 2. Transcripts
        # 3. Lessons & Pages
        # 4. Forum Digests
        kind_priority = {
            "document": 1,
            "transcript": 2,
            "lesson": 3,
            "page": 4,
            "forum_digest": 5,
        }
        gathered.sort(key=lambda item: (kind_priority.get(item.kind, 99), item.title.lower()))

        # Enforce max sources safeguard
        if len(gathered) > max_sources:
            logger.info(
                "Course %s has %d sources, truncating to %d to stay within limit",
                code,
                len(gathered),
                max_sources,
            )
            gathered = gathered[:max_sources]

        return gathered

    def _consolidate_course_overview(self, course_info: dict[str, Any]) -> Optional[CourseFile]:
        """Consolidate Canvas syllabus, overview, and module outlines into a single document."""
        code = course_info["course_code"]
        kb_name = course_info.get("kb_name", "")
        c_dir = self.data_dir / "courses" / kb_name
        pages_dir = c_dir / "pages"
        edstem_dir = c_dir / "edstem"

        sections: list[str] = [
            f"# {code} — Course Overview & Syllabus",
            f"**Course**: {course_info.get('course_name', code)} ({code})",
            f"**Generated**: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}",
            "\n---\n",
        ]

        found_any = False
        # 1. Main course overview from Canvas
        cov = pages_dir / "00-course-overview.md"
        if cov.exists():
            txt = cov.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                sections.append("## Course Information & Syllabus\n")
                sections.append(txt)
                sections.append("\n---\n")
                found_any = True

        # 2. Modules overview if present
        for mod_name in ["10-modules.md", "01-modules.md"]:
            mod_p = pages_dir / mod_name
            if mod_p.exists():
                txt = mod_p.read_text(encoding="utf-8", errors="replace").strip()
                if txt:
                    sections.append("## Modules & Course Structure\n")
                    sections.append(txt)
                    sections.append("\n---\n")
                    found_any = True
                    break

        # 3. EdStem resources / overview if not duplicate
        if edstem_dir.exists():
            res_p = edstem_dir / "20-resources-index.md"
            if res_p.exists():
                txt = res_p.read_text(encoding="utf-8", errors="replace").strip()
                if txt and len(txt) > 200:
                    sections.append("## Supplementary EdStem Resources\n")
                    sections.append(txt)
                    sections.append("\n---\n")
                    found_any = True

        if not found_any:
            return None

        out_dir = self.consolidated_cache_dir / code
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "00_Course_Overview_Syllabus.md"
        out_file.write_text(filter_academic_boilerplate("\n".join(sections)), encoding="utf-8")

        return CourseFile(
            path=out_file,
            rel_path=f"consolidated/{code}_00_Course_Overview_Syllabus.md",
            title=sanitize_title(f"{code} — Course Overview & Syllabus"),
            kind="page",
            size_bytes=out_file.stat().st_size,
            file_hash=compute_sha256(out_file),
            is_generated=True,
        )

    def _consolidate_assignments(self, course_info: dict[str, Any]) -> Optional[CourseFile]:
        """Consolidate Canvas assignment briefs, rubrics, and stubs into an authoritative guide."""
        code = course_info["course_code"]
        kb_name = course_info.get("kb_name", "")
        pages_dir = self.data_dir / "courses" / kb_name / "pages"
        if not pages_dir.exists():
            return None

        assignment_files = sorted(pages_dir.glob("*assignments*.md"))
        if not assignment_files:
            return None

        sections: list[str] = [
            f"# {code} — Assignments & Assessment Guide",
            f"**Course**: {course_info.get('course_name', code)} ({code})",
            f"**Total Assessments Tracked**: {len(assignment_files)}",
            f"**Generated**: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}",
            "\n---\n",
            "## Summary Table of Assessments\n",
            "| Assessment | Points | Submission Type | Details |",
            "| :--- | :--- | :--- | :--- |",
        ]

        details_sections: list[str] = ["\n---\n## Detailed Assessment Briefs & Specifications\n"]

        for af in assignment_files:
            try:
                content = af.read_text(encoding="utf-8", errors="replace").strip()
                lines = content.splitlines()
                title = af.stem.replace("-", " ").title()
                if lines and lines[0].startswith("#"):
                    title = lines[0].lstrip("#").strip()

                pts = "N/A"
                sub_type = "Standard"
                url = ""
                body_lines: list[str] = []

                for l in lines[1:]:
                    sl = l.strip()
                    if sl.lower().startswith("points:"):
                        pts = sl.split(":", 1)[1].strip()
                    elif sl.lower().startswith("submission types:"):
                        sub_type = sl.split(":", 1)[1].strip()
                    elif sl.lower().startswith("canvas url:"):
                        url = sl.split(":", 1)[1].strip()
                    else:
                        body_lines.append(l)

                body = "\n".join(body_lines).strip()
                link_md = f"[Canvas Link]({url})" if url else "-"
                sections.append(f"| **{title}** | {pts} | {sub_type} | {link_md} |")

                if body:
                    details_sections.append(f"### {title}\n")
                    details_sections.append(f"- **Points**: {pts}\n- **Submission Type**: {sub_type}")
                    if url:
                        details_sections.append(f"- **Canvas URL**: {url}")
                    details_sections.append(f"\n{body}\n\n---\n")
            except Exception as exc:
                logger.debug("Error processing assignment file %s: %s", af.name, exc)

        full_doc = filter_academic_boilerplate("\n".join(sections) + "\n" + "\n".join(details_sections))
        out_dir = self.consolidated_cache_dir / code
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "Assignments_Guide.md"
        out_file.write_text(full_doc, encoding="utf-8")

        return CourseFile(
            path=out_file,
            rel_path=f"consolidated/{code}_Assignments_Guide.md",
            title=sanitize_title(f"{code} — Assignments & Assessment Guide"),
            kind="page",
            size_bytes=out_file.stat().st_size,
            file_hash=compute_sha256(out_file),
            is_generated=True,
        )

    def _consolidate_announcements(self, course_info: dict[str, Any]) -> Optional[CourseFile]:
        """Consolidate individual weekly Canvas announcements into a single chronological digest."""
        code = course_info["course_code"]
        kb_name = course_info.get("kb_name", "")
        pages_dir = self.data_dir / "courses" / kb_name / "pages"
        if not pages_dir.exists():
            return None

        announcement_files = sorted(pages_dir.glob("*announcements*.md"))
        if not announcement_files:
            return None

        parsed: list[tuple[str, str, str, str]] = []
        for af in announcement_files:
            try:
                content = af.read_text(encoding="utf-8", errors="replace").strip()
                lines = content.splitlines()
                title = af.stem.replace("-", " ").title()
                if lines and lines[0].startswith("#"):
                    title = lines[0].lstrip("#").strip()

                posted_date = "2026-01-01"
                url = ""
                body_lines: list[str] = []
                for l in lines[1:]:
                    sl = l.strip()
                    if sl.lower().startswith("posted:"):
                        posted_date = sl.split(":", 1)[1].strip()
                    elif sl.lower().startswith("canvas url:"):
                        url = sl.split(":", 1)[1].strip()
                    else:
                        body_lines.append(l)

                parsed.append((posted_date, title, url, "\n".join(body_lines).strip()))
            except Exception:
                pass

        # Sort chronological (oldest to newest)
        parsed.sort(key=lambda x: x[0])

        doc_lines = [
            f"# {code} — Announcements & Course Updates Digest",
            f"**Course**: {course_info.get('course_name', code)} ({code})",
            f"**Announcements Included**: {len(parsed)}",
            f"**Generated**: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}",
            "\n---\n",
        ]

        for pdate, ptitle, purl, pbody in parsed:
            clean_date = pdate[:10] if len(pdate) >= 10 else pdate
            doc_lines.append(f"## [{clean_date}] {ptitle}\n")
            if purl:
                doc_lines.append(f"- **Canvas URL**: {purl}\n")
            doc_lines.append(pbody)
            doc_lines.append("\n\n---\n")

        full_doc = filter_academic_boilerplate("\n".join(doc_lines))
        out_dir = self.consolidated_cache_dir / code
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "Announcements_Digest.md"
        out_file.write_text(full_doc, encoding="utf-8")

        return CourseFile(
            path=out_file,
            rel_path=f"consolidated/{code}_Announcements_Digest.md",
            title=sanitize_title(f"{code} — Announcements & Updates Digest"),
            kind="page",
            size_bytes=out_file.stat().st_size,
            file_hash=compute_sha256(out_file),
            is_generated=True,
        )

    def _consolidate_discussions(self, course_info: dict[str, Any]) -> Optional[CourseFile]:
        """Consolidate Canvas discussion prompts (e.g. BLAW) into a single digest."""
        code = course_info["course_code"]
        kb_name = course_info.get("kb_name", "")
        pages_dir = self.data_dir / "courses" / kb_name / "pages"
        if not pages_dir.exists():
            return None

        disc_files = sorted(pages_dir.glob("*discussions*.md"))
        if not disc_files:
            return None

        doc_lines = [
            f"# {code} — Weekly Discussion Topics Digest",
            f"**Course**: {course_info.get('course_name', code)} ({code})",
            f"**Topics Included**: {len(disc_files)}",
            f"**Generated**: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}",
            "\n---\n",
        ]

        for df in disc_files:
            try:
                content = df.read_text(encoding="utf-8", errors="replace").strip()
                title = df.stem.replace("-", " ").title()
                doc_lines.append(f"## {title}\n")
                doc_lines.append(content)
                doc_lines.append("\n\n---\n")
            except Exception:
                pass

        full_doc = filter_academic_boilerplate("\n".join(doc_lines))
        out_dir = self.consolidated_cache_dir / code
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "Discussions_Digest.md"
        out_file.write_text(full_doc, encoding="utf-8")

        return CourseFile(
            path=out_file,
            rel_path=f"consolidated/{code}_Discussions_Digest.md",
            title=sanitize_title(f"{code} — Weekly Discussions Digest"),
            kind="page",
            size_bytes=out_file.stat().st_size,
            file_hash=compute_sha256(out_file),
            is_generated=True,
        )

    def _gather_canvas_download_documents(self, course_info: dict[str, Any]) -> list[CourseFile]:
        """Collect official course files downloaded via Canvas with content hash deduplication."""
        if not self.manifest_file.exists():
            return []

        try:
            with open(self.manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as exc:
            logger.error("Failed to read downloads_manifest.json: %s", exc)
            return []

        canvas_id = str(course_info.get("canvas_course_id", ""))
        files: list[CourseFile] = []
        seen_doc_hashes: set[str] = set()

        items = manifest.get("items", {})
        for _, item in items.items():
            if str(item.get("course_id", "")) != canvas_id:
                continue

            local_path_str = item.get("local_filepath")
            if not local_path_str:
                continue

            p = Path(local_path_str)
            if not p.exists() or not p.is_file():
                continue

            if p.suffix.lower() not in SUPPORTED_DOC_EXTS:
                continue

            title = item.get("title") or p.name
            size = p.stat().st_size
            if size == 0 or size > 150 * 1024 * 1024:  # NotebookLM 200MB max
                continue

            file_hash = compute_sha256(p)
            if file_hash in seen_doc_hashes:
                # Skip duplicate file contents (e.g. Ermogenous-1.pdf vs Ermogenous.pdf)
                continue
            seen_doc_hashes.add(file_hash)

            files.append(
                CourseFile(
                    path=p,
                    rel_path=f"canvas_downloads/{p.name}",
                    title=sanitize_title(title),
                    kind="document",
                    size_bytes=size,
                    file_hash=file_hash,
                    is_generated=False,
                )
            )

        return files

    def _gather_canvas_pages(self, course_info: dict[str, Any]) -> list[CourseFile]:
        """Collect rendered Canvas markdown pages, excluding pages consolidated into digests."""
        kb_name = course_info.get("kb_name", "")
        code = course_info["course_code"]
        pages_dir = self.data_dir / "courses" / kb_name / "pages"
        if not pages_dir.exists():
            return []

        SKIP_EXACT_NAMES = {
            "00-course-overview.md",
            "01-modules.md",
            "10-modules.md",
            "20-resources-index.md",
            "40-echo360-embeds.md",
            "99-sync-notes.md",
        }

        target_pages_dir = self.pages_cache_dir / code
        target_pages_dir.mkdir(parents=True, exist_ok=True)

        files: list[CourseFile] = []
        for p in sorted(pages_dir.glob("*.md")):
            if not p.is_file():
                continue
            if p.name in SKIP_EXACT_NAMES:
                continue
            # Skip announcements, assignments, and discussions (consolidated into digests)
            lower_name = p.name.lower()
            if "announcements" in lower_name or "assignments" in lower_name or "discussions" in lower_name:
                continue

            # Skip standalone academic integrity / cheating / policy warning pages
            if is_boilerplate_page(p.name):
                logger.debug("Skipping academic boilerplate page %s for %s", p.name, code)
                continue

            size = p.stat().st_size
            if size == 0:
                continue

            content = p.read_text(encoding="utf-8", errors="replace")
            cleaned_content = filter_academic_boilerplate(content)
            if not cleaned_content.strip() or len(cleaned_content.strip()) < 50:
                continue

            clean_md_path = target_pages_dir / p.name
            clean_md_path.write_text(cleaned_content, encoding="utf-8")
            clean_size = clean_md_path.stat().st_size
            file_hash = compute_sha256(clean_md_path)
            title = p.stem.replace("-", " ").title()

            files.append(
                CourseFile(
                    path=clean_md_path,
                    rel_path=f"pages/{p.name}",
                    title=sanitize_title(f"Page — {title}"),
                    kind="page",
                    size_bytes=clean_size,
                    file_hash=file_hash,
                    is_generated=True,
                )
            )
        return files

    def _gather_echo360_transcripts(self, course_info: dict[str, Any]) -> list[CourseFile]:
        """Convert Echo360 WebVTT subtitles into clean, disambiguated Markdown transcripts."""
        echo_id = str(course_info.get("echo360_course_id", ""))
        code = course_info["course_code"]
        subtitles_dir = self.cache_dir / "echo360_subtitles"
        if not subtitles_dir.exists():
            return []

        candidate_vtts: list[tuple[Path, str]] = []

        if self.manifest_file.exists():
            try:
                with open(self.manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                for _, item in manifest.get("items", {}).items():
                    if echo_id and str(item.get("course_id", "")) == echo_id:
                        lp = item.get("local_filepath")
                        if lp and lp.endswith(".vtt"):
                            p = Path(lp)
                            if p.exists():
                                t = item.get("title") or p.stem
                                t = re.sub(r"[\s\-_]*subtitles?\.vtt$", "", t, flags=re.IGNORECASE)
                                candidate_vtts.append((p, t))
            except Exception as exc:
                logger.error("Error reading manifest for echo subtitles: %s", exc)

        if code == "BLAW10001":
            for p in subtitles_dir.glob("BLAW_EMBED_*.vtt"):
                if not any(cv[0] == p for cv in candidate_vtts):
                    candidate_vtts.append((p, f"BLAW Lecture {p.stem}"))

        target_dir = self.transcripts_cache_dir / code
        target_dir.mkdir(parents=True, exist_ok=True)

        files: list[CourseFile] = []
        seen_titles: set[str] = set()

        for vtt_path, raw_title in candidate_vtts:
            clean_md_path = target_dir / f"{vtt_path.stem}.md"
            # Regenerate if not existing or outdated
            if not clean_md_path.exists() or clean_md_path.stat().st_mtime < vtt_path.stat().st_mtime:
                md_content = self._vtt_to_markdown(vtt_path, raw_title, code, course_info)
                if not md_content.strip():
                    continue
                clean_md_path.write_text(filter_academic_boilerplate(md_content), encoding="utf-8")

            size = clean_md_path.stat().st_size
            if size == 0:
                continue

            file_hash = compute_sha256(clean_md_path)
            clean_title = format_lecture_title(code, raw_title, vtt_path.name)

            # Ensure complete title uniqueness across transcripts
            if clean_title in seen_titles:
                h = hashlib.md5(vtt_path.name.encode()).hexdigest()[:4]
                clean_title = f"{clean_title} [{h}]"
            seen_titles.add(clean_title)

            files.append(
                CourseFile(
                    path=clean_md_path,
                    rel_path=f"transcripts/{clean_md_path.name}",
                    title=clean_title,
                    kind="transcript",
                    size_bytes=size,
                    file_hash=file_hash,
                    is_generated=True,
                )
            )

        return files

    def _vtt_to_markdown(
        self,
        vtt_path: Path,
        title: str,
        course_code: str,
        course_info: Optional[dict[str, Any]] = None,
    ) -> str:
        """Convert a WebVTT file into smooth reading markdown paragraphs with direct lecture links."""
        try:
            content = vtt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

        cue_pattern = re.compile(
            r"(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
        )
        lines = content.splitlines()
        paragraphs: list[str] = []
        current_words: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("WEBVTT") or stripped.startswith("NOTE") or stripped.isdigit():
                continue
            if cue_pattern.search(stripped):
                continue
            cleaned = re.sub(r"<[^>]+>", "", stripped).strip()
            if cleaned:
                current_words.append(cleaned)
                if len(" ".join(current_words).split()) >= 80:
                    paragraphs.append(" ".join(current_words))
                    current_words = []

        if current_words:
            paragraphs.append(" ".join(current_words))

        body = "\n\n".join(paragraphs)
        if not body.strip():
            return ""

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        # Resolve lecture web URLs for NotebookLM citations
        links_section: list[str] = []
        cid = course_info.get("canvas_course_id") if course_info else None

        # ── Pipeline A: Regular Echo360 Section Recordings (Timetable class meetings) ──
        if vtt_path.name.startswith("G_"):
            # Reconstruct authentic Echo360 lesson ID by restoring colons in ISO timestamp components
            # Windows filenames cannot contain ':' so the file was saved as T16_05_00.000
            lesson_id = re.sub(r"T(\d{2})_(\d{2})_(\d{2})", r"T\1:\2:\3", vtt_path.stem)
            links_section.append(f"- **Direct Lecture Recording**: https://echo360.net.au/lesson/{lesson_id}/classroom?focus=Video")

            g_match = re.match(r"G_([a-f0-9\-]+)_([a-f0-9\-]+)_", vtt_path.name)
            if g_match:
                section_id = g_match.group(2)
                links_section.append(f"- **Echo360 Subject Section**: https://echo360.net.au/section/{section_id}/home")

            if cid:
                links_section.append(f"- **Canvas Lecture Capture (SSO)**: https://canvas.lms.unimelb.edu.au/courses/{cid}/external_tools/701")

        # ── Pipeline B: Canvas In-Page Video Embeds (Asynchronous clips & seminar videos) ──
        else:
            embed_uuid = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", vtt_path.name)
            if embed_uuid:
                uid = embed_uuid.group(1)
                links_section.append(f"- **Direct Embedded Video**: https://echo360.net.au/media/{uid}/public?autoplay=false&automute=false")

            echo_id = course_info.get("echo360_course_id") if course_info else None
            if echo_id:
                links_section.append(f"- **Echo360 Subject Section**: https://echo360.net.au/section/{echo_id}/home")

            if cid:
                links_section.append(f"- **Canvas Course Portal**: https://canvas.lms.unimelb.edu.au/courses/{cid}")

        links_block = ""
        if links_section:
            links_block = "**Lecture Links**:\n" + "\n".join(links_section) + "\n\n"

        return (
            f"# Lecture Transcript: {title}\n"
            f"**Course**: {course_code} | **Processed**: {now_str}\n\n"
            f"{links_block}"
            f"---\n\n"
            f"{body}\n"
        )

    def _gather_edstem_materials(self, course_info: dict[str, Any]) -> list[CourseFile]:
        """Collect official EdStem lessons and course files with cleaned lesson titles.

        Note: Overview and modules files are skipped because they are integrated into
        the consolidated Course Overview & Syllabus guide.
        """
        kb_name = course_info.get("kb_name", "")
        edstem_dir = self.data_dir / "courses" / kb_name / "edstem"
        if not edstem_dir.exists():
            return []

        files: list[CourseFile] = []

        code = course_info["course_code"]
        lessons_dir = edstem_dir / "lessons"
        if lessons_dir.exists():
            target_lessons_dir = self.lessons_cache_dir / code
            target_lessons_dir.mkdir(parents=True, exist_ok=True)
            for lesson_file in sorted(lessons_dir.glob("*.md")):
                if not lesson_file.is_file():
                    continue
                if is_boilerplate_page(lesson_file.name):
                    continue

                content = lesson_file.read_text(encoding="utf-8", errors="replace")
                cleaned_content = filter_academic_boilerplate(content)
                if not cleaned_content.strip():
                    continue

                clean_md_path = target_lessons_dir / lesson_file.name
                clean_md_path.write_text(cleaned_content, encoding="utf-8")
                size = clean_md_path.stat().st_size
                file_hash = compute_sha256(clean_md_path)
                clean_title = clean_lesson_title(lesson_file.stem)
                files.append(
                    CourseFile(
                        path=clean_md_path,
                        rel_path=f"edstem/lessons/{lesson_file.name}",
                        title=clean_title,
                        kind="lesson",
                        size_bytes=size,
                        file_hash=file_hash,
                        is_generated=True,
                    )
                )

        # 2. Course Files / Slides
        files_dir = edstem_dir / "files"
        if files_dir.exists():
            for f_path in sorted(files_dir.iterdir()):
                if not f_path.is_file() or f_path.suffix.lower() not in SUPPORTED_DOC_EXTS:
                    continue
                size = f_path.stat().st_size
                if size == 0 or size > 150 * 1024 * 1024:
                    continue
                file_hash = compute_sha256(f_path)
                files.append(
                    CourseFile(
                        path=f_path,
                        rel_path=f"edstem/files/{f_path.name}",
                        title=sanitize_title(f"EdStem File — {f_path.name}"),
                        kind="document",
                        size_bytes=size,
                        file_hash=file_hash,
                        is_generated=False,
                    )
                )

        return files

    def _generate_edstem_forum_digests(
        self,
        course_info: dict[str, Any],
        threads_per_digest: int = 75,
    ) -> list[CourseFile]:
        """Consolidate high-volume EdStem forum threads into batched digests.

        Prevents hitting the 300 source cap (e.g. COMP10001 with 1,285 threads)
        by consolidating 75 threads per clean digest markdown file.
        """
        kb_name = course_info.get("kb_name", "")
        code = course_info["course_code"]
        discussions_dir = self.data_dir / "courses" / kb_name / "edstem" / "discussions"
        if not discussions_dir.exists():
            return []

        all_threads = sorted(list(discussions_dir.glob("*.md")))
        if not all_threads:
            return []

        target_dir = self.digests_cache_dir / code
        target_dir.mkdir(parents=True, exist_ok=True)

        digests: list[CourseFile] = []
        batch_num = 1

        for i in range(0, len(all_threads), threads_per_digest):
            chunk = all_threads[i : i + threads_per_digest]
            digest_file = target_dir / f"EdStem_Discussions_Digest_Part_{batch_num:02d}.md"

            # Rebuild digest
            doc_lines = [
                f"# {code} EdStem Forum Discussions Digest — Part {batch_num:02d}",
                f"**Threads Included**: {i + 1} to {i + len(chunk)} of {len(all_threads)}",
                f"**Generated**: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}",
                "",
                "---",
                "",
            ]

            for thread_path in chunk:
                try:
                    thread_text = thread_path.read_text(encoding="utf-8", errors="replace").strip()
                    doc_lines.append(f"## Discussion: {thread_path.stem}\n")
                    doc_lines.append(thread_text)
                    doc_lines.append("\n---\n")
                except Exception as exc:
                    logger.debug("Skipping unreadable thread %s: %s", thread_path.name, exc)

            digest_content = filter_academic_boilerplate("\n".join(doc_lines))
            digest_file.write_text(digest_content, encoding="utf-8")

            size = digest_file.stat().st_size
            file_hash = compute_sha256(digest_file)
            digests.append(
                CourseFile(
                    path=digest_file,
                    rel_path=f"digests/{digest_file.name}",
                    title=sanitize_title(f"EdStem Discussions Digest Part {batch_num:02d}"),
                    kind="forum_digest",
                    size_bytes=size,
                    file_hash=file_hash,
                    is_generated=True,
                )
            )
            batch_num += 1

        return digests
