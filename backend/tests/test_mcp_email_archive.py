import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

# Load the zero-dependency archive module directly: the parent email_monitor
# package imports optional production services unrelated to this offline test.
import importlib.util
archive_path = Path(__file__).resolve().parents[1] / "unisync" / "services" / "email_monitor" / "archive_search.py"
spec = importlib.util.spec_from_file_location("unisync_test_archive", archive_path)
assert spec and spec.loader
archive_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive_module)
EmailArchive = archive_module.EmailArchive


class EmailArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.emails=self.root/'emails'
        self.emails.mkdir()
        self.ledger=self.root/'processed.jsonl'
        self.items=[]

    def add(self, n, body=None, attachments=None, archived=None):
        folder=archived or self.emails / f'msg{n}'
        folder.mkdir(parents=True, exist_ok=True)
        text=body or f'The final exam date has been changed for COMP10001 record {n}'
        (folder/'body.txt').write_text(text,encoding='utf-8')
        meta={'subject':f'Exam and grade notice {n}', 'from':'tutor@example.edu',
              'to':['student@example.edu'], 'date':f'2026-11-05T10:{n%60:02d}:00+11:00',
              'message_id':f'<msg{n}@university.edu>','attachments':attachments or []}
        (folder/'metadata.json').write_text(json.dumps(meta))
        self.items.append({'ledger_id':f'account:message{n}','archived_path':str(folder),
                           'subject':meta['subject'],'status':'processed','processed_at':n})
        self.ledger.write_text(''.join(json.dumps(x)+'\n' for x in self.items))
        return folder

    def test_search_all_not_last_250_and_fetch_body_in_chunks(self):
        for n in range(301):
            self.add(n)
        archive=EmailArchive(self.ledger,self.emails)
        result=archive.search('exam',limit=20)
        self.assertEqual(result['total_matched'],301)
        self.assertEqual(len(result['emails']),20)
        self.assertEqual(result['next_offset'],20)
        result2=archive.search('record 0',limit=5)
        self.assertGreaterEqual(result2['total_matched'],1)
        message=archive.get('account:message0',max_chars=10)
        self.assertEqual(message['body'],'The final ')
        self.assertTrue(message['has_more'])
        more=archive.get('account:message0',offset=10,max_chars=200)
        self.assertIn('COMP10001',more['body'])
        self.assertFalse(more['has_more'])

    def test_new_email_visible_without_manual_index_rebuild(self):
        self.add(0)
        archive=EmailArchive(self.ledger,self.emails)
        self.assertEqual(archive.search('new unique')['total_matched'],0)
        self.add(1,'This is a new unique text for indexing')
        self.assertEqual(archive.search('new unique')['total_matched'],1)

    def test_attachment_is_guarded_and_readable(self):
        folder=self.emails/'msg1'
        folder.mkdir(parents=True)
        attdir=folder/'attachments'
        attdir.mkdir()
        (attdir/'rubric.txt').write_text('Marks: 100 out of 100')
        atts=[{'filename':'rubric.txt','local_filepath':str(attdir/'rubric.txt'),
               'size_bytes':21,'content_type':'text/plain'}]
        self.add(1,attachments=atts,archived=folder)
        archive=EmailArchive(self.ledger,self.emails)
        self.assertIn('Marks: 100',archive.attachment('account:message1',0)['content'])
        self.assertEqual(archive.get('account:message1')['attachments'][0]['filename'],'rubric.txt')
        self.assertEqual(archive.attachment('account:message1',4)['error'],'Attachment index not found')
        # Tamper archive metadata: rejected even if an external file exists.
        atts[0]['local_filepath']=str(self.root/'secret.txt')
        (self.root/'secret.txt').write_text('secret')
        self.add(2,attachments=atts)
        self.assertIn('invalid',archive.attachment('account:message2',0)['error'].lower())

    def test_untrusted_archive_path_is_never_indexed(self):
        outside=self.root/'outside';outside.mkdir()
        self.add(1,archived=outside)
        archive=EmailArchive(self.ledger,self.emails)
        self.assertEqual(archive.search('exam')['total_matched'],0)
        self.assertIn('not found',archive.get('account:message1')['error'].lower())


    def test_does_not_scan_partial_jsonl_record_until_complete(self):
        self.add(0)
        archive = EmailArchive(self.ledger, self.emails)
        self.assertEqual(archive.search('exam')['total_matched'], 1)
        folder = self.emails / 'msg1'
        folder.mkdir()
        (folder / 'body.txt').write_text('critical meeting postponed')
        (folder / 'metadata.json').write_text(json.dumps({'subject':'Revised meeting','from':'tutor@example.edu'}))
        entry = json.dumps({'ledger_id':'account:message1','archived_path':str(folder),
                            'subject':'Revised meeting','status':'processed','processed_at':123})
        with self.ledger.open('ab') as out:
            out.write(entry.encode())
        self.assertEqual(archive.search('critical')['total_matched'], 0)
        with self.ledger.open('ab') as out:
            out.write(b'\n')
        self.assertEqual(archive.search('critical')['total_matched'], 1)

    def test_truncated_ledger_rebuilds_without_ghost_messages(self):
        self.add(0)
        self.add(1)
        archive = EmailArchive(self.ledger, self.emails)
        self.assertEqual(archive.search()['total_matched'], 2)
        self.ledger.write_text(json.dumps(self.items[0]) + '\n')
        self.assertEqual(archive.search()['total_matched'], 1)

    def test_fts_operators_are_not_executable_query_syntax(self):
        self.add(0, body='"exam" OR OR 123 COMP10001')
        archive = EmailArchive(self.ledger, self.emails)
        for query in ['exam OR', '"exam"', 'COMP10001', 'exam NOT']:
            self.assertIn('emails', archive.search(query))
        self.assertEqual(archive.search('no-such-needle')['total_matched'], 0)

    def test_single_string_recipients_do_not_drop_archived_email(self):
        self.add(0, body='Important policy reminder')
        folder = self.emails / 'msg0'
        metadata = json.loads((folder / 'metadata.json').read_text())
        metadata.update({'to':'student@example.edu','cc':None,'from':{'name':'Prof'}})
        (folder / 'metadata.json').write_text(json.dumps(metadata))
        archive = EmailArchive(self.ledger, self.emails)
        self.assertEqual(archive.search('reminder')['total_matched'], 1)
        self.assertEqual(archive.get('account:message0')['from'], 'Prof')


if __name__=='__main__':unittest.main()
