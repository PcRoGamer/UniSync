"""Core orchestration service for Google NotebookLM integration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from notebooklm.client import NotebookLMClient
    from notebooklm.paths import get_storage_path
except ImportError:
    NotebookLMClient = None
    get_storage_path = None

from unisync.core.text_cleaner import is_boilerplate_page
from unisync.services.notebooklm.gatherer import CourseFileGatherer, CourseFile
from unisync.services.notebooklm.state import (
    load_sync_state,
    save_sync_state,
    get_course_sync_state,
    update_course_notebook,
    record_uploaded_source,
    is_source_synced,
)

logger = logging.getLogger(__name__)


class NotebookLMAuthError(Exception):
    """Raised when NotebookLM authentication is missing or invalid."""
    pass


class NotebookLMService:
    """Service managing Google NotebookLM subject notebooks and source uploads."""

    def __init__(self, project_root: Optional[Path] = None):
        self.gatherer = CourseFileGatherer(project_root=project_root)

    @staticmethod
    def get_storage_file_path() -> Path:
        """Return the default storage_state.json path for NotebookLM session."""
        if get_storage_path is not None:
            try:
                return get_storage_path()
            except Exception:
                pass
        return Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"

    def is_authenticated(self) -> bool:
        """Check if active Google session credentials exist in storage."""
        storage_path = self.get_storage_file_path()
        if not storage_path.exists():
            return False
        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            # Must have google auth cookies (e.g. SID, __Secure-1PSID)
            has_sid = any(c.get("name") in ("SID", "__Secure-1PSID") for c in cookies)
            return bool(has_sid or len(cookies) > 0)
        except Exception:
            return False

    def get_auth_status(self) -> dict[str, Any]:
        """Return detailed authentication diagnostics."""
        storage_path = self.get_storage_file_path()
        authenticated = self.is_authenticated()
        state = load_sync_state()

        email = state.get("account", {}).get("email")
        if authenticated and not email and storage_path.exists():
            try:
                with open(storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Attempt to extract email from storage state if present
                for c in data.get("cookies", []):
                    if "account" in c.get("name", "").lower():
                        email = c.get("value")
                        break
            except Exception:
                pass

        return {
            "authenticated": authenticated,
            "storage_path": str(storage_path),
            "account_email": email,
            "tier": state.get("account", {}).get("tier", "pro"),
            "max_sources_per_notebook": state.get("account", {}).get("max_sources_per_notebook", 300),
            "max_notebooks": state.get("account", {}).get("max_notebooks", 500),
            "login_instructions": "Run 'notebooklm login' in your terminal or trigger browser sign-in via the UniSync dashboard.",
        }

    @staticmethod
    def clean_stale_browser_locks() -> None:
        """Clear stale lockfiles from the browser profile directory."""
        try:
            profile_dir = Path.home() / ".notebooklm" / "profiles" / "default" / "browser_profile"
            lockfile = profile_dir / "lockfile"
            if lockfile.exists():
                try:
                    lockfile.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def trigger_browser_login_process(self, timeout: int = 300) -> None:
        """Clean browser locks and prepare profile for interactive terminal login."""
        self.clean_stale_browser_locks()

    def get_sync_status(self) -> dict[str, Any]:
        """Return overall NotebookLM synchronization state across all subjects."""
        auth_status = self.get_auth_status()
        state = load_sync_state()
        enrolled_courses = self.gatherer.get_enrolled_academic_courses()

        courses_summary: list[dict[str, Any]] = []
        for code, info in enrolled_courses.items():
            stored_nb = state.get("notebooks", {}).get(code, {})
            sources = stored_nb.get("sources", {})
            courses_summary.append({
                "course_code": code,
                "course_name": info.get("course_name", code),
                "notebook_id": stored_nb.get("notebook_id"),
                "notebook_title": stored_nb.get("notebook_title"),
                "notebook_url": stored_nb.get("notebook_url"),
                "last_synced_at": stored_nb.get("last_synced_at"),
                "synced_sources_count": len(sources),
                "is_provisioned": bool(stored_nb.get("notebook_id")),
            })

        return {
            "auth": auth_status,
            "courses": courses_summary,
            "total_courses": len(courses_summary),
            "updated_at": state.get("updated_at"),
        }

    async def get_or_create_subject_notebooks(
        self,
        client: Optional[NotebookLMClient] = None,
    ) -> dict[str, dict[str, Any]]:
        """Find or create dedicated NotebookLM notebooks for all enrolled academic courses."""
        if not self.is_authenticated():
            raise NotebookLMAuthError("NotebookLM session not authenticated. Run 'notebooklm login' first.")

        enrolled = self.gatherer.get_enrolled_academic_courses()
        state = load_sync_state()

        async def _process(cl: NotebookLMClient) -> dict[str, dict[str, Any]]:
            # Retrieve user email if available
            try:
                email = await cl.get_account_email()
                if email:
                    state["account"]["email"] = email
                    save_sync_state(state)
            except Exception:
                pass

            # List existing notebooks from Google
            existing_notebooks = await cl.notebooks.list()
            notebooks_by_title = {nb.title.strip(): nb for nb in existing_notebooks}

            provisioned: dict[str, dict[str, Any]] = {}
            for code, info in enrolled.items():
                course_name = info.get("course_name", code)
                expected_title = f"{code} — {course_name}"
                stored = state.get("notebooks", {}).get(code, {})
                stored_id = stored.get("notebook_id")

                # Check if already linked and still exists remotely
                matched_nb = None
                if stored_id:
                    matched_nb = next((nb for nb in existing_notebooks if nb.id == stored_id), None)

                # If not matched by ID, try matching by course code in title
                if not matched_nb:
                    for t, nb in notebooks_by_title.items():
                        if code in t:
                            matched_nb = nb
                            break

                # If still not found, create new notebook on Google NotebookLM
                if not matched_nb:
                    logger.info("Creating new NotebookLM notebook for %s: '%s'", code, expected_title)
                    matched_nb = await cl.notebooks.create(title=expected_title)
                    notebooks_by_title[matched_nb.title] = matched_nb

                # Update local tracking record
                updated = update_course_notebook(
                    course_code=code,
                    notebook_id=matched_nb.id,
                    notebook_title=matched_nb.title,
                    course_name=course_name,
                )
                provisioned[code] = updated

            return provisioned

        if client is not None:
            return await _process(client)

        async with NotebookLMClient.from_storage(allow_headless=False) as cl:
            return await _process(cl)

    async def purge_obsolete_sources(
        self,
        course_code: str,
        client: Optional[NotebookLMClient] = None,
    ) -> dict[str, Any]:
        """Purge duplicate stubs and obsolete sources from Google NotebookLM and sync state."""
        code = course_code.upper()
        state = load_sync_state()
        cdata = state.get("notebooks", {}).get(code, {})
        notebook_id = cdata.get("notebook_id")
        if not notebook_id:
            return {"course_code": code, "deleted": 0, "errors": ["No notebook ID found"]}

        desired_files = self.gatherer.gather_all_sources(code, max_sources=500)
        desired_rel_paths = {cf.rel_path for cf in desired_files}

        async def _do_purge(cl: NotebookLMClient) -> dict[str, Any]:
            remote_sources = await cl.sources.list(notebook_id=notebook_id)
            deleted_count = 0
            errors = []
            sources_to_delete = []

            for s in remote_sources:
                stitle = s.title.strip()
                is_raw_md = bool(re.match(r"^\d+-lessons-.*\.md$", stitle))
                is_worksheet_stub = bool("Worksheet" in stitle and "Assignments" in stitle and "Page" in stitle)
                is_old_transcript = bool("Transcript -" in stitle and any(r in stitle for r in ["TH B02", "TH 106"]))
                is_overview_stub = bool(
                    any(
                        ov in stitle
                        for ov in [
                            "EdStem - 00 Course Overview",
                            "EdStem - 01 Modules",
                            "EdStem - 20 Resources Index",
                            "99 Sync Notes",
                            "Page - 00 Course Overview",
                            "Page - 01 Modules",
                            "Page - 10 Modules",
                            "Page - 20 Resources Index",
                            "Page - 99 Sync Notes",
                        ]
                    )
                )
                is_ermogenous_dupe = bool(stitle == "Ermogenous-1.pdf")
                is_old_lesson_title = bool(stitle.startswith("Lesson -") and "Lessons" in stitle)
                is_standalone_announcement_page = bool("Announcements" in stitle and stitle.startswith("Page -"))
                is_standalone_discussion_page = bool("Discussions" in stitle and stitle.startswith("Page -"))
                is_standalone_assignment_page = bool("Assignments" in stitle and stitle.startswith("Page -"))
                is_boilerplate_stub = bool(is_boilerplate_page(stitle))

                if (
                    is_raw_md
                    or is_worksheet_stub
                    or is_old_transcript
                    or is_overview_stub
                    or is_ermogenous_dupe
                    or is_old_lesson_title
                    or is_standalone_announcement_page
                    or is_standalone_discussion_page
                    or is_standalone_assignment_page
                    or is_boilerplate_stub
                ):
                    sources_to_delete.append(s)

            logger.info("Found %d obsolete sources to purge from %s", len(sources_to_delete), code)
            for s in sources_to_delete:
                try:
                    logger.info("Deleting obsolete source from %s: '%s' (id=%s)", code, s.title, s.id)
                    await cl.sources.delete(notebook_id=notebook_id, source_id=s.id)
                    deleted_count += 1
                except Exception as exc:
                    logger.warning("Failed to delete source %s: %s", s.id, exc)
                    errors.append(f"{s.title}: {exc}")

            # Clean sync state in notebooklm_sync.json
            current_state = load_sync_state()
            nb_sources = current_state.get("notebooks", {}).get(code, {}).get("sources", {})
            deleted_ids = {s.id for s in sources_to_delete}
            updated_sources = {
                rp: sinfo
                for rp, sinfo in nb_sources.items()
                if rp in desired_rel_paths and sinfo.get("source_id") not in deleted_ids
            }
            current_state["notebooks"][code]["sources"] = updated_sources
            save_sync_state(current_state)

            return {
                "course_code": code,
                "notebook_id": notebook_id,
                "deleted": deleted_count,
                "remaining_desired": len(desired_files),
                "errors": errors,
            }

        if client is not None:
            return await _do_purge(client)
        async with NotebookLMClient.from_storage(allow_headless=False) as cl:
            return await _do_purge(cl)

    async def sync_course(
        self,
        course_code: str,
        force: bool = False,
        max_sources: int = 290,
    ) -> dict[str, Any]:
        """Synchronize course materials to its dedicated Google NotebookLM notebook."""
        if not self.is_authenticated():
            raise NotebookLMAuthError("NotebookLM session not authenticated. Run 'notebooklm login' first.")

        code = course_code.upper()
        # 1. Gather all files
        files = self.gatherer.gather_all_sources(code, max_sources=max_sources)

        result_summary = {
            "course_code": code,
            "notebook_id": None,
            "total_gathered": len(files),
            "purged": 0,
            "uploaded": 0,
            "skipped": 0,
            "errors": [],
        }

        async with NotebookLMClient.from_storage(allow_headless=False) as client:
            # 2. Ensure notebook exists
            notebooks = await self.get_or_create_subject_notebooks(client=client)
            course_record = notebooks.get(code)
            if not course_record or not course_record.get("notebook_id"):
                raise ValueError(f"Could not provision or find notebook for course {code}")

            notebook_id = course_record["notebook_id"]
            result_summary["notebook_id"] = notebook_id

            # 2b. Purge obsolete stubs/duplicates first
            purge_result = await self.purge_obsolete_sources(code, client=client)
            result_summary["purged"] = purge_result.get("deleted", 0)

            # 3. Upload files with concurrency pool and asynchronous processing
            # Setting wait=False streams files directly to Google in ~1s and lets Google Cloud
            # index them in parallel, avoiding an 18-second sequential polling stall per file.
            sem = asyncio.Semaphore(3)
            state_lock = asyncio.Lock()

            async def _upload_worker(cf: CourseFile) -> None:
                if not force and is_source_synced(code, cf.rel_path, cf.file_hash):
                    result_summary["skipped"] += 1
                    return

                # If a previous version exists in sync state with a different hash,
                # delete the old version remotely to prevent duplicate sources
                current_state = load_sync_state()
                existing_record = current_state.get("notebooks", {}).get(code, {}).get("sources", {}).get(cf.rel_path)
                if existing_record and existing_record.get("source_id"):
                    old_sid = existing_record["source_id"]
                    try:
                        logger.info("Replacing stale remote source for %s: '%s' (id=%s)", code, cf.title, old_sid)
                        await client.sources.delete(notebook_id=notebook_id, source_id=old_sid)
                    except Exception as exc:
                        logger.debug("Failed deleting old source version %s: %s", old_sid, exc)

                async with sem:
                    try:
                        logger.info(
                            "Uploading to NotebookLM [%s]: %s (%s, %d bytes)",
                            code,
                            cf.title,
                            cf.kind,
                            cf.size_bytes,
                        )
                        source = await client.sources.add_file(
                            notebook_id=notebook_id,
                            file_path=cf.path,
                            title=cf.title,
                            wait=False,
                        )
                        async with state_lock:
                            record_uploaded_source(
                                course_code=code,
                                file_rel_path=cf.rel_path,
                                source_id=source.id,
                                file_hash=cf.file_hash,
                                title=cf.title,
                                size_bytes=cf.size_bytes,
                            )
                            result_summary["uploaded"] += 1
                    except Exception as exc:
                        logger.warning("Failed uploading %s to notebook %s: %s", cf.path.name, code, exc)
                        result_summary["errors"].append({
                            "file": cf.rel_path,
                            "title": cf.title,
                            "error": str(exc),
                        })
                        err_str = str(exc).lower()
                        if "authenticat" in err_str or "login" in err_str or "401" in err_str or "expired" in err_str:
                            raise NotebookLMAuthError(f"NotebookLM authentication expired: {exc}") from exc

            await asyncio.gather(*[_upload_worker(cf) for cf in files])

        return result_summary

    async def sync_all_courses(
        self,
        force: bool = False,
        max_sources: int = 290,
    ) -> dict[str, Any]:
        """Synchronize all enrolled academic courses in sequence."""
        if not self.is_authenticated():
            raise NotebookLMAuthError("NotebookLM session not authenticated. Run 'notebooklm login' first.")

        enrolled = self.gatherer.get_enrolled_academic_courses()
        results: list[dict[str, Any]] = []

        for code in enrolled:
            try:
                res = await self.sync_course(code, force=force, max_sources=max_sources)
                results.append(res)
            except NotebookLMAuthError as auth_err:
                logger.error("Authentication expired during course %s, stopping: %s", code, auth_err)
                results.append({
                    "course_code": code,
                    "error": str(auth_err),
                    "uploaded": 0,
                    "skipped": 0,
                })
                break
            except Exception as exc:
                logger.error("Failed sync for course %s: %s", code, exc)
                results.append({
                    "course_code": code,
                    "error": str(exc),
                    "uploaded": 0,
                    "skipped": 0,
                })

        return {
            "synced_at": load_sync_state().get("updated_at"),
            "results": results,
            "total_courses": len(results),
        }
