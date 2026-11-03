"""Pydantic models for the email monitor feature."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EmailMonitorSettings(BaseModel):
    """Persisted to data/user/settings/email_monitor.json"""

    enabled: bool = False
    imap_host: str = "outlook.office365.com"
    imap_port: int = 993
    imap_use_ssl: bool = True
    username: str = ""
    password: str = ""  # masked in API responses
    mailbox: str = "INBOX"
    poll_interval_seconds: int = 300  # min 60
    sender_filter: str = ""  # optional glob like *@unimelb.edu.au
    subject_filter: str = ""  # optional keyword like assignment
    mark_seen: bool = False  # use BODY.PEEK[] by default
    consent_granted: bool = False
    auth_mode: Literal["imap", "graph", "browser_bridge", "thunderbird"] = "imap"
    bootstrap_last_n: int = 0  # 0 = don't bootstrap; max 50


class EmailAttachment(BaseModel):
    """Represents a downloaded attachment from an email."""

    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    content: bytes | None = None  # binary bytes during fetch/download
    local_filepath: str | None = None  # absolute path once saved to disk


class RawMessage(BaseModel):
    """Represents a fetched email with full metadata and attachments."""

    model_config = {"populate_by_name": True}

    message_id: str | None = None  # RFC 822 Message-ID header
    uid: str = ""  # IMAP UID or browser bridge identifier
    subject: str = "(No Subject)"
    from_: str = Field(default="University Sender", alias="from")
    to: list[str] = []
    cc: list[str] = []
    date: datetime | None = None
    text_body: str = ""
    html_body: str | None = None
    headers: dict[str, str] = {}  # key raw headers for provenance & fallback dedup
    attachments: list[EmailAttachment] = []
    raw_eml: bytes | None = None  # complete raw RFC822 .eml bytes

    @field_validator("from_", mode="before")
    @classmethod
    def _normalize_from(cls, val: Any) -> str:
        """Handle string or Graph/OWA object formats for the 'from' address."""
        if isinstance(val, dict):
            addr = val.get("emailAddress", {})
            if isinstance(addr, dict):
                address = addr.get("address", "")
                name = addr.get("name", "")
                if name and address and name != address:
                    return f"{name} <{address}>"
                return address or name or "University Sender"
            return str(val.get("name") or val.get("address") or "University Sender")
        return str(val or "University Sender")


class MailCursor(BaseModel):
    """Persistent cursor for incremental polling."""

    last_uid: int = 0
    last_poll_at: float = 0.0  # time.time()
    uidvalidity: int | None = None  # detect mailbox reset


class LedgerEntry(BaseModel):
    """One entry in the processed.jsonl ledger."""

    ledger_id: str  # f"{account_fingerprint}:{message_key}"
    message_uid: str
    message_id: str | None
    subject: str
    status: Literal["processed", "skipped_no_action", "error"]
    error_message: str = ""
    processed_at: float  # time.time()
    created_todo_ids: list[str] = []
    archived_path: str | None = None
    attachment_count: int = 0


class ConnectionResult(BaseModel):
    """Result of an IMAP connection attempt."""

    success: bool
    message: str = ""
    folder_info: str | None = None  # UIDVALIDITY, EXISTS, etc.
