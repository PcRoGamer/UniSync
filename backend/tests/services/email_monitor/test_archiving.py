"""Tests for programmatic email archiving, attachment saving, and zero-todo guarantees."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path

from unisync.services.email_monitor.models import EmailMonitorSettings, RawMessage, EmailAttachment
from unisync.services.email_monitor.store import EmailMonitorStore
from unisync.services.email_monitor.service import EmailMonitorService
from unisync.services.email_monitor.connectors.fake import FakeConnector
from unisync.core.download_registry import get_download_registry
from unisync.services.todos.service import TodoService


@pytest.mark.asyncio
async def test_email_archiving_saves_attachments_and_zero_todos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that email monitor downloads full message, saves attachments, and creates 0 todos."""
    store = EmailMonitorStore(base_dir=str(tmp_path / "email_monitor"), settings_dir=str(tmp_path / "settings"))
    store.save_settings(
        EmailMonitorSettings(
            enabled=True,
            consent_granted=True,
            imap_host="fake.imap.uni.edu",
            username="student@student.unimelb.edu.au",
            password="pwd",
            mailbox="INBOX",
        )
    )

    emails_dir = tmp_path / "emails"
    emails_dir.mkdir()

    # Monkeypatch path_service to use tmp_path
    from unisync.core import path_service
    monkeypatch.setattr(path_service.get_path_service(), "get_workspace_feature_dir", lambda f: emails_dir if f == "emails" else tmp_path / f)

    fake_conn = FakeConnector()
    attachment_bytes = b"%PDF-1.4 Assignment brief content"
    raw_eml_bytes = b"Subject: COMP30027 Assignment 1\nFrom: lecturer@unimelb.edu.au\n\nPlease find the attached spec."

    msg = RawMessage(
        uid="101",
        subject="COMP30027 Assignment 1 Spec Released",
        from_="lecturer@unimelb.edu.au",
        to=["student@student.unimelb.edu.au"],
        date=datetime(2026, 8, 10, 14, 0, 0, tzinfo=timezone.utc),
        text_body="Please find the attached spec.",
        html_body="<p>Please find the attached spec.</p>",
        raw_eml=raw_eml_bytes,
        attachments=[
            EmailAttachment(
                filename="assignment1_spec.pdf",
                content_type="application/pdf",
                size_bytes=len(attachment_bytes),
                content=attachment_bytes,
            )
        ],
    )
    fake_conn.add_message(msg)

    service = EmailMonitorService(store=store)
    monkeypatch.setattr(service, "_build_connector", lambda s=None: fake_conn)

    # Run poll
    result = await service.poll_now()

    assert result["success"] is True
    assert result["messages_fetched"] == 1
    assert result["todos_created"] == 0  # STRICT ZERO EMAIL TODOS
    assert result["attachments_downloaded"] == 1

    # Verify files saved on disk
    archived_dirs = list(emails_dir.iterdir())
    assert len(archived_dirs) == 1
    msg_dir = archived_dirs[0]

    assert (msg_dir / "message.eml").exists()
    assert (msg_dir / "message.eml").read_bytes() == raw_eml_bytes
    assert (msg_dir / "body.txt").exists()
    assert (msg_dir / "body.txt").read_text(encoding="utf-8") == "Please find the attached spec."
    assert (msg_dir / "body.html").exists()
    assert (msg_dir / "metadata.json").exists()

    att_path = msg_dir / "attachments" / "assignment1_spec.pdf"
    assert att_path.exists()
    assert att_path.read_bytes() == attachment_bytes

    # Verify registration in DownloadRegistry
    reg = get_download_registry()
    item = reg.resolve("assignment1_spec.pdf")
    assert item is not None
    assert item.platform == "email"
    assert item.course_name == "University Email"
