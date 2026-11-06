"""Zero-LLM search and retrieval for UniSync's archived university emails.

Builds a durable SQLite FTS5 index from the *whole* processed.jsonl ledger.
Only reads email archive files under the configured emails root. No inbox login,
network call, content execution, AI classifier, or user task mutation occurs.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import unescape
from pathlib import Path
from typing import Any



def _display_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(_display_text(part) for part in value)
    if isinstance(value, dict):
        return _display_text(value.get("address") or value.get("emailAddress") or value.get("name"))
    return str(value)


class EmailArchive:
    def __init__(self, ledger_path: str | Path, emails_root: str | Path):
        self.ledger = Path(ledger_path)
        self.root = Path(emails_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "email-search.sqlite"

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY, archive_path TEXT NOT NULL, subject TEXT NOT NULL,
            sender TEXT NOT NULL, recipients TEXT NOT NULL, sent_at TEXT NOT NULL,
            processed_at REAL NOT NULL, body TEXT NOT NULL, meta TEXT NOT NULL,
            attachment_names TEXT NOT NULL
        )""")
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS email_fts USING fts5(
            subject, sender, recipients, body, attachment_names, tokenize='unicode61'
        )""")
        return db

    def _trusted_archive(self, path: str | None) -> Path | None:
        if not path:
            return None
        p = Path(path).resolve()
        if not p.is_relative_to(self.root) or not p.is_dir():
            return None
        return p

    @staticmethod
    def _body(folder: Path) -> str:
        txt = folder / "body.txt"
        if txt.is_file():
            return txt.read_text(encoding="utf-8", errors="replace")
        eml = folder / "message.eml"
        if eml.is_file():
            try:
                parsed = BytesParser(policy=policy.default).parsebytes(eml.read_bytes())
                part = parsed.get_body(preferencelist=("plain",)) if parsed.is_multipart() else parsed
                if part:
                    value = part.get_content()
                    if isinstance(value, str):
                        return value
            except Exception:
                pass
        html = folder / "body.html"
        if html.is_file():
            raw = html.read_text(encoding="utf-8", errors="replace")
            return unescape(re.sub(r"<[^>]*>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)))
        return ""

    def sync(self, db: sqlite3.Connection) -> None:
        """Index only complete newly appended JSONL records.

        A truncated or same-sized replaced ledger rebuilds the index. A partially
        written final line is reconsidered on the next query rather than lost.
        """
        if not self.ledger.is_file():
            return
        st = self.ledger.stat()
        signature = f"{st.st_size}:{st.st_mtime_ns}"
        old = db.execute("SELECT value FROM settings WHERE key='ledger_signature'").fetchone()
        if old and old[0] == signature:
            return

        with db:
            row = db.execute("SELECT value FROM settings WHERE key='ledger_size'").fetchone()
            previous = int(row[0]) if row else 0
            reset = st.st_size < previous or (st.st_size == previous and
                       bool(old and old[0] != signature))
            if reset or not previous:
                db.execute("DELETE FROM email_fts")
                db.execute("DELETE FROM emails")
                previous = 0

            # Binary cursor offsets remain correct with UTF-8, including CRLF
            # and non-ASCII message metadata.
            with self.ledger.open("rb") as stream:
                stream.seek(previous)
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        stream.seek(line_start)
                        break
                    try:
                        item = json.loads(line)
                    except (UnicodeDecodeError, ValueError, TypeError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    if item.get("status") != "processed" or not isinstance(item.get("ledger_id"), str):
                        continue
                    ident = item["ledger_id"]
                    if not ident or len(ident) > 512 or db.execute(
                        "SELECT 1 FROM emails WHERE id=?", (ident,)
                    ).fetchone():
                        continue
                    folder = self._trusted_archive(item.get("archived_path"))
                    if not folder:
                        continue

                    db.execute("SAVEPOINT email_index_record")
                    try:
                        meta_file = folder / "metadata.json"
                        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
                        if not isinstance(meta, dict):
                            meta = {}
                        attachments = meta.get("attachments")
                        if not isinstance(attachments, list):
                            attachments = []
                            meta["attachments"] = []
                        names = " ".join(_display_text(a.get("filename")) for a in attachments if isinstance(a, dict))
                        subject = _display_text(meta.get("subject") or item.get("subject"))
                        sender = _display_text(meta.get("from"))
                        recipients = _display_text(meta.get("to")) + " " + _display_text(meta.get("cc"))
                        received = _display_text(meta.get("date"))
                        processed_at = float(item.get("processed_at") or 0)
                        if not received and processed_at:
                            received = datetime.fromtimestamp(processed_at, tz=timezone.utc).isoformat()
                        body = self._body(folder)
                        db.execute("""INSERT INTO emails
                            (id,archive_path,subject,sender,recipients,sent_at,processed_at,body,meta,attachment_names)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (ident,str(folder),subject,sender,recipients,received,processed_at,
                             body,json.dumps(meta,ensure_ascii=False),names))
                        rid = db.execute("SELECT rowid FROM emails WHERE id=?", (ident,)).fetchone()[0]
                        db.execute("""INSERT INTO email_fts
                            (rowid,subject,sender,recipients,body,attachment_names)
                            VALUES(?,?,?,?,?,?)""", (rid,subject,sender,recipients,body,names))
                        db.execute("RELEASE SAVEPOINT email_index_record")
                    except (OSError, ValueError, TypeError, sqlite3.Error):
                        db.execute("ROLLBACK TO SAVEPOINT email_index_record")
                        db.execute("RELEASE SAVEPOINT email_index_record")
                        continue
                complete_offset = stream.tell()
            for key, value in (
                ("ledger_size",str(complete_offset)),
                ("ledger_signature",signature if complete_offset == st.st_size else "incomplete"),
            ):
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key,value))

    @staticmethod
    def _match_expression(query: str) -> str:
        # Literal prefix terms only. User input cannot inject SQLite MATCH operators.
        terms = re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)[:16]
        return " AND ".join('"' + t.replace('"', '""') + '"*' for t in terms)

    def search(self, query: str = "", *, sender: str = "", since: str = "", until: str = "",
               limit: int = 20, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        offset = max(0, min(int(offset), 1_000_000))
        with self._connect() as db:
            self.sync(db)
            expression = self._match_expression(query)
            where, args = [], []
            source = "emails e"
            if expression:
                source = "email_fts JOIN emails e ON e.rowid=email_fts.rowid"
                where.append("email_fts MATCH ?")
                args.append(expression)
            if sender:
                where.append("e.sender LIKE ? ESCAPE '\\'")
                args.append("%" + sender.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            if since:
                where.append("e.sent_at >= ?")
                args.append(since)
            if until:
                where.append("e.sent_at <= ?")
                args.append(until)
            w = (" WHERE " + " AND ".join(where)) if where else ""
            total = db.execute(f"SELECT COUNT(*) FROM {source}{w}", args).fetchone()[0]
            rows = db.execute(f"""SELECT e.id,e.subject,e.sender,e.recipients,e.sent_at,e.processed_at,
                e.body,e.meta FROM {source}{w}
                ORDER BY e.sent_at DESC,e.processed_at DESC LIMIT ? OFFSET ?""", [*args,limit,offset]).fetchall()
            result = []
            for row in rows:
                body = row["body"]
                low = body.casefold()
                positions = [low.find(t) for t in re.findall(r"[^\W_]+", query.casefold()) if t in low]
                start = max(0, min(positions) - 90) if positions else 0
                excerpt = body[start:start+350]
                meta = json.loads(row["meta"])
                result.append({"id": row["id"], "subject": row["subject"], "from": row["sender"],
                               "to": meta.get("to") or [], "date": row["sent_at"],
                               "attachments_count": len(meta.get("attachments") or []),
                               "snippet": ("…" if start else "") + excerpt + ("…" if start+350 < len(body) else "")})
            return {"query": query,"total_matched": total,"offset": offset,
                    "next_offset": offset+len(result) if offset+len(result)<total else None,
                    "emails": result}

    def get(self, email_id: str, *, offset: int = 0, max_chars: int = 12000,
            format: str = "text") -> dict[str, Any]:
        max_chars = max(1, min(int(max_chars), 30000))
        offset = max(0, int(offset))
        with self._connect() as db:
            self.sync(db)
            row = db.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
            if not row:
                return {"error": "Email not found in the UniSync archive"}
            folder = self._trusted_archive(row["archive_path"])
            if not folder:
                return {"error": "Email archive path is no longer accessible"}
            meta = json.loads(row["meta"])
            if format == "eml":
                eml = folder / "message.eml"
                if not eml.is_file():
                    return {"error": "Original RFC822 content is not available for this email"}
                body = eml.read_bytes().decode("utf-8", errors="replace")
            elif format == "text":
                body = row["body"]
            else:
                return {"error": "Unsupported format; use text or eml"}
            attachments = [{"index": i, "filename": str(a.get("filename", "")),
                            "content_type": a.get("content_type", ""), "size_bytes": a.get("size_bytes", 0)}
                           for i,a in enumerate(meta.get("attachments") or []) if isinstance(a,dict)]
            end = offset+max_chars
            return {"id": email_id, "subject": row["subject"], "from": row["sender"],
                    "to": meta.get("to") or [], "cc": meta.get("cc") or [], "date": row["sent_at"],
                    "message_id": meta.get("message_id"), "format": format,
                    "body": body[offset:end], "total_chars": len(body),
                    "offset": offset, "has_more": end<len(body),
                    "next_offset": end if end<len(body) else None,
                    "attachments": attachments}

    def attachment(self, email_id: str, index: int, *, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        max_chars = max(1, min(int(max_chars), 30000))
        offset = max(0, int(offset))
        with self._connect() as db:
            self.sync(db)
            row = db.execute("SELECT archive_path, meta FROM emails WHERE id=?", (email_id,)).fetchone()
            if not row:
                return {"error": "Email not found"}
            folder = self._trusted_archive(row["archive_path"])
            if not folder:
                return {"error": "Untrusted email archive path"}
            atts = (json.loads(row["meta"]).get("attachments") or [])
            if not isinstance(index,int) or index<0 or index>=len(atts) or not isinstance(atts[index],dict):
                return {"error": "Attachment index not found"}
            att = atts[index]
            path = Path(str(att.get("local_filepath") or "")).resolve()
            allowed = (folder / "attachments").resolve()
            if not path.is_relative_to(allowed) or not path.is_file():
                return {"error": "Attachment path is invalid or unavailable"}
            suffix = path.suffix.lower()
            if suffix in {".txt",".md",".csv",".json",".log",".py",".html",".xml",".ics",".vtt"}:
                content = path.read_text(encoding="utf-8",errors="replace")
            elif suffix == ".pdf":
                try:
                    from pypdf import PdfReader
                    content = "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
                except ImportError:
                    return {"error": "PDF extraction requires pypdf"}
            elif suffix == ".docx":
                try:
                    from docx import Document
                    doc = Document(str(path))
                    content = "\n".join([p.text for p in doc.paragraphs] +
                                        [" | ".join(c.text for c in row.cells) for t in doc.tables for row in t.rows])
                except ImportError:
                    return {"error": "DOCX extraction requires python-docx"}
            elif suffix == ".pptx":
                try:
                    from pptx import Presentation
                    slides = Presentation(str(path)).slides
                    content = "\n".join(shape.text for slide in slides for shape in slide.shapes if shape.has_text_frame)
                except ImportError:
                    return {"error": "PPTX extraction requires python-pptx"}
            else:
                return {"id": email_id,"attachment_index": index,"filename": path.name,
                        "binary": True,"size_bytes": path.stat().st_size,
                        "note": "Binary attachment archived; no text extraction available"}
            end=offset+max_chars
            return {"id": email_id,"attachment_index": index,"filename": path.name,
                    "binary": False,"content": content[offset:end],"total_chars":len(content),
                    "offset":offset,"has_more":end<len(content),
                    "next_offset":end if end<len(content) else None}
