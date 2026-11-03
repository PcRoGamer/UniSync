"""EmailMonitorService — poll university inbox, download emails and attachments, manage lifecycle.

Purely programmatic and deterministic — zero LLM calls.
Downloads complete raw RFC822 messages (.eml), metadata JSON, and all attachments to disk.
Registers all downloaded assets in UniSync's DownloadRegistry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from unisync.core.download_registry import get_download_registry
from unisync.core.path_service import get_path_service
from unisync.services.email_monitor.connectors.imap import ImapConnector
from unisync.services.email_monitor.models import (
    ConnectionResult,
    EmailMonitorSettings,
    LedgerEntry,
    MailCursor,
    RawMessage,
)
from unisync.services.email_monitor.extractor import extract_from_message
from unisync.services.email_monitor.summarizer import generate_summary
from unisync.services.todos import get_todo_service
from unisync.services.email_monitor.store import (
    EmailMonitorStore,
    get_email_monitor_store,
    make_ledger_id,
)

logger = logging.getLogger(__name__)


class EmailMonitorService:
    """Poll university inbox, download emails and attachments, manage lifecycle."""

    def __init__(self, store: EmailMonitorStore | None = None):
        self.store = store or get_email_monitor_store()
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._last_poll_at: float | None = None
        self._poll_count: int = 0
        self._todos_created_count: int = 0
        self._attachments_downloaded_count: int = 0

    def _build_connector(
        self, settings: EmailMonitorSettings | None = None
    ) -> Any:
        """Create the appropriate email connector based on auth_mode."""
        s = settings or self.store.get_settings()
        if s.auth_mode == "thunderbird":
            from unisync.services.email_monitor.connectors.thunderbird import (
                ThunderbirdConnector,
            )
            return ThunderbirdConnector(s)
        return ImapConnector(s)

    async def sync_thunderbird(self) -> dict[str, Any]:
        """Sync emails directly from the local Mozilla Thunderbird profile."""
        from unisync.services.email_monitor.connectors.thunderbird import (
            ThunderbirdConnector,
            find_thunderbird_inbox_msf,
        )

        inbox_path = find_thunderbird_inbox_msf()
        if not inbox_path or not inbox_path.exists():
            return {
                "success": False,
                "error": "No Thunderbird profile or Inbox.msf found on this computer.",
            }

        connector = ThunderbirdConnector(msf_path=inbox_path)
        cursor = self.store.load_cursor()
        messages, new_cursor = await asyncio.to_thread(
            connector.list_new_messages, cursor
        )

        ingest_res = self.ingest_messages(
            messages=messages,
            source_host="thunderbird.local",
            username=self.store.get_settings().username or "thunderbird_user",
            mailbox="INBOX",
            source_label="thunderbird_profile",
        )
        return {
            "success": True,
            "inbox_path": str(inbox_path),
            **ingest_res,
        }

    async def start(self) -> None:
        """Start the background poll loop if enabled."""
        settings = self.store.get_settings()
        if not settings.enabled or not settings.consent_granted:
            logger.info(
                "Email monitor not enabled or consent not granted — not starting"
            )
            return

        if self._running:
            logger.warning("Email monitor already running")
            return

        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="email-monitor:poll",
        )
        logger.info("Email monitor poll loop started")

    async def stop(self) -> None:
        """Stop the background poll loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        logger.info("Email monitor poll loop stopped")

    async def poll_now(self) -> dict[str, Any]:
        """Trigger a single poll cycle (used by API 'Check now')."""
        async with self._poll_lock:
            return await self._poll_once()

    async def summarize(
        self, since_days: int = 1, mailbox: str | None = None
    ) -> dict[str, Any]:
        """Generate a programmatic summary of recent emails (deterministic, no LLM)."""
        settings = self.store.get_settings()
        connector = self._build_connector(settings)

        until = datetime.now(timezone.utc)
        since = until - timedelta(days=since_days)

        try:
            messages = await asyncio.to_thread(
                connector.fetch_range, since, until, limit=100
            )
        except Exception as e:
            logger.error("Failed to fetch messages for summary: %s", e)
            return {"success": False, "error": str(e)}

        date_label = f"{since.date().isoformat()} to {until.date().isoformat()}"
        try:
            digest = await generate_summary(messages, date_label)
        except Exception as e:
            logger.error("Failed to generate summary: %s", e)
            return {"success": False, "error": str(e)}

        date_key = until.date().isoformat()
        self.store.save_summary(f"digest-{date_key}", digest)

        return {"success": True, "summary": digest, "message_count": len(messages)}

    def get_status(self) -> dict[str, Any]:
        """Return current status."""
        settings = self.store.get_settings()
        cursor = self.store.load_cursor()
        return {
            "enabled": settings.enabled,
            "consent_granted": settings.consent_granted,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
            "poll_count": self._poll_count,
            "todos_created": 0,  # Programmatic architecture: 0 email todos
            "attachments_downloaded": self._attachments_downloaded_count,
            "cursor": cursor.model_dump(mode="json"),
            "running": self._running,
        }

    # ── Poll loop ───────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Background poll loop."""
        while self._running:
            try:
                settings = self.store.get_settings()
                interval = max(settings.poll_interval_seconds, 60)

                async with self._poll_lock:
                    await self._poll_once()

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll loop error: %s", e, exc_info=True)
                self._last_error = str(e)
                await asyncio.sleep(60)

    async def _poll_once(self) -> dict[str, Any]:
        """One poll cycle: fetch → save .eml & attachments → update registry & ledger."""
        settings = self.store.get_settings()
        if not settings.enabled or not settings.consent_granted:
            return {"success": False, "reason": "Not enabled or consent not granted"}

        connector = self._build_connector(settings)
        cursor = self.store.load_cursor()

        result: dict[str, Any] = {
            "success": True,
            "messages_fetched": 0,
            "todos_created": 0,
            "attachments_downloaded": 0,
            "errors": [],
        }

        try:
            messages, new_cursor = await asyncio.to_thread(
                connector.list_new_messages, cursor
            )
        except Exception as e:
            error = f"IMAP fetch failed: {e}"
            logger.error(error)
            self._last_error = error
            return {"success": False, "error": error}

        result["messages_fetched"] = len(messages)

        # Archive, register in DownloadRegistry, and update ledger
        ingest_res = self.ingest_messages(
            messages=messages,
            source_host=settings.imap_host,
            username=settings.username,
            mailbox=settings.mailbox,
            source_label="imap",
        )
        result["attachments_downloaded"] = ingest_res.get("attachments_count", 0)
        result["errors"].extend(ingest_res.get("errors", []))

        # Save cursor
        new_cursor.last_poll_at = time.time()
        self.store.save_cursor(new_cursor)

        # Update service state
        self._last_poll_at = time.time()
        self._last_error = None
        self._poll_count += 1

        return result

    def ingest_messages(
        self,
        messages: list[RawMessage],
        source_host: str = "outlook.cloud.microsoft",
        username: str = "student",
        mailbox: str = "INBOX",
        source_label: str = "outlook_web",
    ) -> dict[str, Any]:
        """Process and archive a batch of messages (from IMAP or browser bridge).

        Zero LLM calls. Deterministic archiving of body.txt, body.html, metadata.json,
        and registration in DownloadRegistry and persistent processed.jsonl ledger.
        """
        emails_base_dir = get_path_service().get_workspace_feature_dir("emails")
        emails_base_dir.mkdir(parents=True, exist_ok=True)
        download_registry = get_download_registry()

        ingested = 0
        skipped = 0
        attachments_count = 0
        errors: list[str] = []

        for msg in messages:
            headers_dict = msg.headers or {}
            # Ensure deterministic non-empty UID
            if not msg.uid:
                import hashlib
                msg.uid = f"msg_{hashlib.sha256((msg.subject + (msg.text_body or '')).encode('utf-8')).hexdigest()[:12]}"

            ledger_id = make_ledger_id(
                imap_host=source_host,
                username=username,
                mailbox=mailbox,
                message_id=msg.message_id,
                uid=msg.uid,
                headers=headers_dict,
                subject=msg.subject,
                date=msg.date.isoformat() if msg.date else None,
            )

            # Skip if already processed (deduplication)
            if self.store.is_processed(ledger_id):
                skipped += 1
                continue

            try:
                # 1. Deterministic directory structure
                date_str = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "undated"
                safe_subj = re.sub(r'[\/*?:"<>|\s]+', "_", msg.subject[:40]).strip("_") or "no_subject"
                # Strip special characters from uid for filesystem safety
                safe_uid = re.sub(r'[\/*?:"<>|]+', "", msg.uid)[:24] or "msg"
                msg_dir = emails_base_dir / f"{date_str}_{safe_subj}_{safe_uid}"
                msg_dir.mkdir(parents=True, exist_ok=True)

                # 2. Save raw RFC822 .eml if available
                eml_path = msg_dir / "message.eml"
                if msg.raw_eml:
                    eml_path.write_bytes(msg.raw_eml)

                # 3. Save text & html bodies
                if msg.text_body:
                    (msg_dir / "body.txt").write_text(msg.text_body, encoding="utf-8")
                if msg.html_body:
                    (msg_dir / "body.html").write_text(msg.html_body, encoding="utf-8")

                # 4. Save attachments
                saved_atts: list[dict[str, Any]] = []
                if msg.attachments:
                    att_dir = msg_dir / "attachments"
                    att_dir.mkdir(parents=True, exist_ok=True)
                    for att in msg.attachments:
                        att_path = att_dir / att.filename
                        if att.content:
                            att_path.write_bytes(att.content)
                        att.local_filepath = str(att_path)
                        saved_atts.append({
                            "filename": att.filename,
                            "content_type": att.content_type,
                            "size_bytes": att.size_bytes,
                            "local_filepath": str(att_path),
                        })

                        # Register attachment in UniSync DownloadRegistry
                        download_registry.register(
                            platform="email",
                            item_id=f"email:{msg.uid}:{att.filename}",
                            title=f"{att.filename} (Email: {msg.subject[:30]})",
                            direct_link=f"mailbox://{username}/{mailbox}#{msg.uid}?attachment={att.filename}",
                            local_filepath=att_path,
                            course_id="email",
                            course_name="University Email",
                            size_bytes=att.size_bytes,
                            content_type=att.content_type,
                            metadata={"from": msg.from_, "subject": msg.subject},
                        )

                # 5. Save structured metadata.json
                meta = {
                    "uid": msg.uid,
                    "message_id": msg.message_id,
                    "subject": msg.subject,
                    "from": msg.from_,
                    "to": msg.to,
                    "cc": msg.cc,
                    "date": msg.date.isoformat() if msg.date else None,
                    "headers": msg.headers,
                    "attachments": saved_atts,
                    "source": source_label,
                }
                (msg_dir / "metadata.json").write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
                )

                # Register the email item itself in DownloadRegistry
                body_path = msg_dir / "body.txt" if (msg_dir / "body.txt").exists() else (eml_path if eml_path.exists() else msg_dir / "metadata.json")
                download_registry.register(
                    platform="email",
                    item_id=f"email:{msg.uid}",
                    title=f"Email: {msg.subject or 'No Subject'}",
                    direct_link=f"https://outlook.cloud.microsoft/mail/id/{msg.uid}" if "outlook" in source_host else f"mailbox://{username}/{mailbox}#{msg.uid}",
                    local_filepath=body_path,
                    course_id="email",
                    course_name="University Email",
                    size_bytes=len(msg.text_body.encode("utf-8")) if msg.text_body else (len(msg.raw_eml) if msg.raw_eml else 0),
                    content_type="text/plain" if (msg_dir / "body.txt").exists() else "message/rfc822",
                    metadata={"from": msg.from_, "date": msg.date.isoformat() if msg.date else None},
                )

                # 6. Record in persistent ledger
                self.store.mark_processed(
                    LedgerEntry(
                        ledger_id=ledger_id,
                        message_uid=msg.uid,
                        message_id=msg.message_id,
                        subject=msg.subject,
                        status="processed",
                        processed_at=time.time(),
                        archived_path=str(msg_dir),
                        attachment_count=len(msg.attachments),
                    )
                )

                ingested += 1
                attachments_count += len(saved_atts)
                self._attachments_downloaded_count += len(saved_atts)

            except Exception as e:
                error = f"Failed to process message {msg.uid}: {e}"
                logger.warning(error)
                errors.append(error)
                self.store.mark_processed(
                    LedgerEntry(
                        ledger_id=ledger_id,
                        message_uid=msg.uid,
                        message_id=msg.message_id,
                        subject=msg.subject,
                        status="error",
                        error_message=str(e),
                        processed_at=time.time(),
                    )
                )

        # Flush download registry index to disk
        download_registry.flush()

        return {
            "success": len(errors) == 0,
            "received": len(messages),
            "ingested": ingested,
            "skipped": skipped,
            "attachments_count": attachments_count,
            "errors": errors,
        }


# ── Singleton factory ──────────────────────────────────────────────

_service_instance: EmailMonitorService | None = None


def get_email_monitor_service() -> EmailMonitorService:
    global _service_instance
    if _service_instance is None:
        _service_instance = EmailMonitorService()
    return _service_instance


class _EmailMonitorServiceProxy:
    """Convenience proxy so callers can write ``email_monitor_service.poll_now()``."""

    def __getattr__(self, name: str):
        return getattr(get_email_monitor_service(), name)


email_monitor_service: EmailMonitorService = _EmailMonitorServiceProxy()  # type: ignore[assignment]
