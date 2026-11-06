"""Offline regression tests: email ingestion must not silently lose messages."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unisync.services.email_monitor.models import (
    EmailAttachment, EmailMonitorSettings, LedgerEntry, MailCursor, RawMessage
)
from unisync.services.email_monitor.store import EmailMonitorStore
from unisync.services.email_monitor.service import EmailMonitorService
from unisync.services.email_monitor.connectors.imap import ImapConnector


class CaptureRegistry:
    def __init__(self):
        self.items=[]
    def register(self,**kwargs):
        self.items.append(kwargs)
    def flush(self):
        pass


class TestSourceDurability(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.store=EmailMonitorStore(base_dir=str(self.base/'monitor'), settings_dir=str(self.base/'settings'))
        self.store.save_settings(EmailMonitorSettings(
            enabled=True, consent_granted=True, imap_host="imap.fake.example",
            username="student@example.edu",password="dummy",mailbox="INBOX"))

    def test_error_ledgers_are_retryable_across_restart(self):
        ident="account:failed"
        failure=LedgerEntry(ledger_id=ident,message_uid="9",message_id="<m9>",
                            subject="Assignment",status="error",error_message="disk full",processed_at=1)
        self.store.mark_processed(failure)
        self.assertFalse(self.store.is_processed(ident))
        second=EmailMonitorStore(base_dir=str(self.base/'monitor'),settings_dir=str(self.base/'settings'))
        self.assertFalse(second.is_processed(ident))
        second.mark_processed(failure.model_copy(update={"status":"processed"}))
        self.assertTrue(second.is_processed(ident))

    def test_poll_does_not_commit_cursor_when_archive_fails(self):
        self.store.save_cursor(MailCursor(last_uid=7,uidvalidity=88))
        service=EmailMonitorService(store=self.store)
        class Fake:
            def list_new_messages(self,cursor):
                return [RawMessage(uid="8",subject="Important",from_="tutor@example.edu",text_body="Exam")], MailCursor(last_uid=8,uidvalidity=88)
        service._build_connector=lambda settings=None: Fake()
        service.ingest_messages=lambda **kw: {
            "success":False,"received":1,"ingested":0,
            "skipped":0,"attachments_count":0,"errors":["UID 8: disk write failed"]}
        result=asyncio.run(service.poll_now())
        self.assertFalse(result["success"])
        self.assertEqual(self.store.load_cursor().last_uid,7)
        self.assertIn("disk write failed",service.get_status()["last_error"])
        service.ingest_messages=lambda **kw: {
            "success":True,"received":1,"ingested":1,
            "skipped":0,"attachments_count":0,"errors":[]}
        result=asyncio.run(service.poll_now())
        self.assertTrue(result["success"])
        self.assertEqual(self.store.load_cursor().last_uid,8)

    def test_imap_uidvalidity_reset_rereads_existing_messages(self):
        cfg=self.store.get_settings()
        connector=ImapConnector(cfg)
        seen=[]
        class Client:
            def response(self,cmd):
                return ("OK",[b"UIDVALIDITY 2"])
            def uid(self,operation,*args):
                if operation=="SEARCH":
                    seen.append(args[-1])
                    return ("OK",[b"1 2"])
                uid=args[0]
                return ("OK",[(b"UID "+uid,b"Subject: Hello\n\nBody")])
            def logout(self): pass
        connector._connect=lambda: Client()
        connector._parse_fetched_message=lambda data,uid:RawMessage(uid=uid,subject="Hello",from_="tutor@example.edu",text_body="Body")
        messages,cursor=connector.list_new_messages(MailCursor(last_uid=500,uidvalidity=1))
        self.assertEqual(seen,["UID 1:*"])
        self.assertEqual([m.uid for m in messages],["1","2"])
        self.assertEqual(cursor.last_uid,2)
        self.assertEqual(cursor.uidvalidity,2)

    def test_imap_fetch_failure_raises_instead_of_skipping_uid(self):
        connector=ImapConnector(self.store.get_settings())
        class Client:
            def response(self,cmd):
                return ("OK",[b"UIDVALIDITY 1"])
            def uid(self,operation,*args):
                if operation=="SEARCH":
                    return ("OK",[b"4 5"])
                return ("NO",[])
            def logout(self): pass
        connector._connect=lambda: Client()
        with self.assertRaisesRegex(ConnectionError,"UID 4"):
            connector.list_new_messages(MailCursor(last_uid=3,uidvalidity=1))

    def test_attachment_path_is_sanitized_and_collision_safe(self):
        root=self.base/"archive"
        registry=CaptureRegistry()
        service=EmailMonitorService(store=self.store)
        class PathService:
            def get_workspace_feature_dir(self,name):
                return root
        with patch("unisync.services.email_monitor.service.get_path_service",return_value=PathService()), \
             patch("unisync.services.email_monitor.service.get_download_registry",return_value=registry):
            message=RawMessage(uid="10",message_id="<msg10>",subject="Results",
                               from_="tutor@example.edu",text_body="Hello",
                               attachments=[
                                   EmailAttachment(filename="../escape.txt",content=b"first",size_bytes=5),
                                   EmailAttachment(filename="escape.txt",content=b"second",size_bytes=6)])
            outcome=service.ingest_messages([message])
        self.assertTrue(outcome["success"],outcome)
        self.assertFalse((self.base/"escape.txt").exists())
        emails=list(root.glob("*/metadata.json"))
        self.assertEqual(len(emails),1)
        meta=json.loads(emails[0].read_text())
        filenames=[x["filename"] for x in meta["attachments"]]
        self.assertEqual(filenames,["escape.txt","escape_2.txt"])
        for name in filenames:
            self.assertTrue((emails[0].parent/"attachments"/name).is_file())


if __name__=="__main__":
    unittest.main()
