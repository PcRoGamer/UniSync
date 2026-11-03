"""Unit and integration tests for ThunderbirdConnector and Mork msf parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from unisync.services.email_monitor.connectors.thunderbird import (
    ThunderbirdConnector,
    find_thunderbird_inbox_msf,
    parse_mork_msf,
    unescape_mork,
)
from unisync.services.email_monitor.models import MailCursor, RawMessage
from unisync.services.email_monitor.service import EmailMonitorService
from unisync.services.email_monitor.store import EmailMonitorStore


def test_unescape_mork():
    """Test hex unescaping, UTF-8 multi-byte decoding, and syntax cleanup."""
    # Plain text with escaped parens
    s1 = r"Video Games \(Remaking Reality\)"
    assert unescape_mork(s1) == "Video Games (Remaking Reality)"

    # Hex escaped ascii
    s2 = "Subject$20With$20Spaces"
    assert unescape_mork(s2) == "Subject With Spaces"

    # Multi-byte UTF-8 (e.g. en-dash \u2013 is $E2$80$93)
    s3 = "Title$20$E2$80$93$20Subtitle"
    assert unescape_mork(s3) == "Title \u2013 Subtitle"


def test_parse_mork_msf_synthetic(tmp_path: Path):
    """Test parsing a synthetic Mork .msf file."""
    msf_file = tmp_path / "Inbox.msf"

    # Minimal valid Mork structure with cells and message rows
    content = """// <!-- <mdb:mork:z v="1.4"/> -->
< <(a=c)>
  (80=ns:msg:db:row:scope:msgs:all)(81=subject)(82=sender)(83=message-id)
  (85=recipients)(86=date)(B1=ewsId)(B7=preview)>
<(88=Lecturer <lecturer@unimelb.edu.au>)(89=COMP10001 Assignment 1 Release)
  (8A=Student <student@unimelb.edu.au>)(91=AAMkTestItem001)(93=Assignment 1 is now live.)>
{1:^80 {(k^96:c)(s=9)}
  [1(^86=68BC8E6E)(^83=msg001@unimelb)(^82^88)(^81^89)(^85^8A)(^B1^91)(^B7^93)]
}
// Folder info / settings row (should be filtered out)
[daysToKeepHdrs(^C7=daysToKeepHdrs)]
"""
    msf_file.write_text(content, encoding="latin1")

    msgs = parse_mork_msf(msf_file)
    assert len(msgs) == 1

    msg = msgs[0]
    assert msg.subject == "COMP10001 Assignment 1 Release"
    assert msg.from_ == "Lecturer <lecturer@unimelb.edu.au>"
    assert msg.to == ["Student <student@unimelb.edu.au>"]
    assert msg.uid == "AAMkTestItem001"
    assert msg.text_body == "Assignment 1 is now live."
    assert msg.date is not None
    assert msg.date.year == 2025 or msg.date.year == 2026


def test_thunderbird_connector_methods(tmp_path: Path):
    """Test connector test_connection, list_new_messages, and fetch_range."""
    msf_file = tmp_path / "Inbox.msf"
    content = """// <!-- <mdb:mork:z v="1.4"/> -->
<(88=Tutor <tutor@unimelb.edu.au>)(89=Week 2 Consultation Hours)(93=Consultations start tomorrow.)>
{1:^80 {(k^96:c)(s=9)}
  [1(^86=68BC8E6E)(^82^88)(^81^89)(^B7^93)]
}
"""
    msf_file.write_text(content, encoding="latin1")

    connector = ThunderbirdConnector(msf_path=msf_file)

    # 1. test_connection
    res = connector.test_connection()
    assert res.success is True
    assert "Found Thunderbird Inbox" in res.message

    # 2. list_new_messages
    msgs, cursor = connector.list_new_messages(MailCursor())
    assert len(msgs) == 1
    assert msgs[0].subject == "Week 2 Consultation Hours"
    assert cursor.last_uid == 1

    # 3. fetch_range
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    until = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ranged = connector.fetch_range(since, until)
    assert len(ranged) == 1


def test_sync_thunderbird_integration(tmp_path: Path, monkeypatch):
    """Test EmailMonitorService.sync_thunderbird with mock profile."""
    base_dir = tmp_path / "email_monitor"
    settings_dir = tmp_path / "settings"
    emails_dir = tmp_path / "emails"

    store = EmailMonitorStore(base_dir=str(base_dir), settings_dir=str(settings_dir))
    service = EmailMonitorService(store=store)

    from unisync.core import path_service
    ps = path_service.get_path_service()
    monkeypatch.setattr(ps, "get_workspace_feature_dir", lambda name: emails_dir if name == "emails" else base_dir)

    msf_file = tmp_path / "Inbox.msf"
    content = """// <!-- <mdb:mork:z v="1.4"/> -->
<(88=Prof <prof@unimelb.edu.au>)(89=BLAW10001 Final Exam Details)(93=Exam date confirmed for Nov 15.)>
{1:^80 {(k^96:c)(s=9)}
  [1(^86=68BC8E6E)(^82^88)(^81^89)(^B7^93)]
}
"""
    msf_file.write_text(content, encoding="latin1")

    # Monkeypatch find_thunderbird_inbox_msf to return our test file
    from unisync.services.email_monitor.connectors import thunderbird
    monkeypatch.setattr(thunderbird, "find_thunderbird_inbox_msf", lambda override_path=None: msf_file)

    import asyncio
    res = asyncio.run(service.sync_thunderbird())

    assert res["success"] is True
    assert res["received"] == 1
    assert res["ingested"] == 1

    # Verify directory created on disk
    folders = list(emails_dir.iterdir())
    assert len(folders) == 1
    assert "BLAW10001" in folders[0].name
    assert (folders[0] / "body.txt").read_text(encoding="utf-8") == "Exam date confirmed for Nov 15."
