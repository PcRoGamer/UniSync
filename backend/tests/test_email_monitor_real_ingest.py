"""Integration tests for EmailMonitorService browser bridge ingestion and MCP search."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unisync.mcp.server import unisync_search_announcements
from unisync.services.email_monitor.models import RawMessage
from unisync.services.email_monitor.service import EmailMonitorService
from unisync.services.email_monitor.store import EmailMonitorStore


def test_email_monitor_ingest_and_deduplication(tmp_path: Path, monkeypatch):
    """Test full cycle: ingest messages -> verify files on disk -> verify deduplication."""
    base_dir = tmp_path / "email_monitor"
    settings_dir = tmp_path / "settings"
    emails_dir = tmp_path / "emails"

    store = EmailMonitorStore(base_dir=str(base_dir), settings_dir=str(settings_dir))
    service = EmailMonitorService(store=store)

    from unisync.core import path_service
    ps = path_service.get_path_service()
    monkeypatch.setattr(ps, "get_workspace_feature_dir", lambda name: emails_dir if name == "emails" else base_dir)

    msg1 = RawMessage(
        uid="msg_test_001",
        subject="COMP10001 Mid-semester exam room allocation",
        from_="Examiner (exams@unimelb.edu.au)",
        date=datetime.now(timezone.utc),
        text_body="Please note that student IDs ending in 0-4 are allocated to Wilson Hall.",
    )
    msg2 = RawMessage(
        uid="msg_test_002",
        subject="BLAW10001 Assignment 2 Extension Notice",
        from_="Professor Law (law@unimelb.edu.au)",
        date=datetime.now(timezone.utc),
        text_body="Due to public holidays, Assignment 2 is extended by 48 hours.",
    )

    res = service.ingest_messages(
        messages=[msg1, msg2],
        source_host="outlook.office.com",
        username="test_student",
        source_label="outlook_web",
    )

    assert res["success"] is True
    assert res["ingested"] == 2
    assert res["skipped"] == 0

    email_folders = list(emails_dir.iterdir())
    assert len(email_folders) == 2

    comp_folder = next(f for f in email_folders if "COMP10001" in f.name)
    assert (comp_folder / "body.txt").exists()
    assert (comp_folder / "metadata.json").exists()
    assert "Wilson Hall" in (comp_folder / "body.txt").read_text(encoding="utf-8")

    meta = json.loads((comp_folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["uid"] == "msg_test_001"
    assert meta["source"] == "outlook_web"

    # Test Deduplication: Ingest the exact same messages again
    res2 = service.ingest_messages(
        messages=[msg1, msg2],
        source_host="outlook.office.com",
        username="test_student",
        source_label="outlook_web",
    )

    assert res2["success"] is True
    assert res2["ingested"] == 0
    assert res2["skipped"] == 2


def test_mcp_announcements_search_with_ingested_email(tmp_path: Path, monkeypatch):
    """Test that unisync_search_announcements retrieves ingested emails and extracts snippets."""
    base_dir = tmp_path / "email_monitor"
    settings_dir = tmp_path / "settings"
    emails_dir = tmp_path / "emails"

    store = EmailMonitorStore(base_dir=str(base_dir), settings_dir=str(settings_dir))
    service = EmailMonitorService(store=store)

    from unisync.core import path_service
    ps = path_service.get_path_service()
    monkeypatch.setattr(ps, "get_workspace_feature_dir", lambda name: emails_dir if name == "emails" else base_dir)

    msg = RawMessage(
        uid="exam_announcement_99",
        subject="COMP10001 Final Examination Venue Announcement",
        from_="Head Tutor (tutor@unimelb.edu.au)",
        date=datetime.now(timezone.utc),
        text_body="The final exam will take place in the Royal Exhibition Building on October 20.",
    )
    service.ingest_messages(
        messages=[msg],
        source_host="outlook.office.com",
        username="student@unimelb.edu.au",
        source_label="outlook_web",
    )

    from unisync.services.email_monitor import store as em_store
    monkeypatch.setattr(em_store, "EmailMonitorStore", lambda *args, **kwargs: store)

    result_str = unisync_search_announcements(query="Royal Exhibition")
    result = json.loads(result_str)

    assert result["total_matched"] >= 1
    item = next((a for a in result["announcements"] if "Final Examination" in a["subject"]), None)
    assert item is not None
    assert "Royal Exhibition" in item["snippet"]
    assert "tutor@unimelb.edu.au" in item["sender"]
