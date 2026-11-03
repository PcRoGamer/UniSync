"""Thunderbird Local Profile Email Connector.

Extracts university emails directly from local Mozilla Thunderbird profile databases
(Inbox.msf / Mork format) with zero network calls, zero credentials, and zero app passwords.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unisync.services.email_monitor.models import (
    ConnectionResult,
    EmailMonitorSettings,
    MailCursor,
    RawMessage,
)

logger = logging.getLogger(__name__)


def find_thunderbird_inbox_msf(override_path: str | Path | None = None) -> Path | None:
    """Locate the active university Outlook/Exchange Inbox.msf file across OS paths."""
    if override_path:
        p = Path(override_path)
        if p.is_file():
            return p
        if p.is_dir():
            cand = p / "Inbox.msf"
            if cand.exists():
                return cand

    search_roots: list[Path] = []
    system = platform.system()

    if system == "Windows":
        app_data = os.environ.get("APPDATA")
        if app_data:
            search_roots.append(Path(app_data) / "Thunderbird" / "Profiles")
    elif system == "Darwin":
        search_roots.append(Path.home() / "Library" / "Thunderbird" / "Profiles")
    else:  # Linux / Unix
        search_roots.append(Path.home() / ".thunderbird")

    for root in search_roots:
        if not root.exists():
            continue
        # Search for Exchange or IMAP Office 365 / unimelb inbox files first
        office_inboxes = list(root.glob("**/Mail/outlook.office365.com/Inbox.msf"))
        if office_inboxes:
            return office_inboxes[0]

        unimelb_inboxes = list(root.glob("**/Mail/*unimelb*/Inbox.msf"))
        if unimelb_inboxes:
            return unimelb_inboxes[0]

        imap_inboxes = list(root.glob("**/ImapMail/*office365*/INBOX.msf")) or list(
            root.glob("**/ImapMail/*office365*/Inbox.msf")
        )
        if imap_inboxes:
            return imap_inboxes[0]

        # Generic fallback
        any_inboxes = list(root.glob("**/Inbox.msf")) or list(root.glob("**/INBOX.msf"))
        if any_inboxes:
            return any_inboxes[0]

    return None


def unescape_mork(s: str) -> str:
    """Unescape Mork hex tokens ($XX) and strip syntax escapes, decoding UTF-8 properly."""
    byte_arr = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "$" and i + 2 < len(s):
            try:
                byte_arr.append(int(s[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        byte_arr.extend(s[i].encode("latin1", errors="replace"))
        i += 1

    try:
        decoded = byte_arr.decode("utf-8")
    except UnicodeDecodeError:
        decoded = byte_arr.decode("latin1", errors="replace")

    return (
        decoded.replace("\\)", ")")
        .replace("\\(", "(")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_mork_msf(msf_path: Path) -> list[RawMessage]:
    """Parse a Thunderbird Mail Summary File (.msf) in Mork format.

    Extracts subject, sender, RFC message-id, received timestamp, EWS ID,
    recipients, and plain text body preview snippet.
    """
    if not msf_path.exists():
        logger.warning("Thunderbird msf file not found: %s", msf_path)
        return []

    try:
        raw = msf_path.read_text(encoding="latin1", errors="replace")
    except Exception as exc:
        logger.error("Failed to read Thunderbird msf file %s: %s", msf_path, exc)
        return []

    # Clean multi-line line continuations in Mork format
    cleaned = re.sub(r"\\\r?\n\s*", "", raw)

    # 1. Build dictionary of all value cells: (hexId = value)
    # Values can contain escaped parentheses \( or \)
    cells: dict[str, str] = {}
    cell_pattern = re.compile(r"\(([0-9A-Fa-f]+)\s*=\s*([^\\)]*(?:\\.[^\\)]*)*)\)")
    for m in cell_pattern.finditer(cleaned):
        cid = m.group(1).upper()
        cells[cid] = unescape_mork(m.group(2).strip())

    # 2. Extract message rows: [id(^col^val)(^col=val)...]
    # Standard Mork column IDs for Thunderbird:
    # 81: subject, 82: sender, 83: message-id, 85: recipients, 86: date,
    # B1: ewsId, B7: preview, B4: dateReceived
    row_pattern = re.compile(r"\[([0-9A-Fa-f-]+)\s*((?:\([^\)]+\)\s*)+)\]")

    messages: list[RawMessage] = []
    seen_uids: set[str] = set()

    for m in row_pattern.finditer(cleaned):
        row_id = m.group(1)
        content = m.group(2)

        # Must have subject (^81) or EWS ID (^B1) or preview (^B7)
        if "^81" not in content and "^B1" not in content and "^B7" not in content:
            continue

        props: dict[str, str] = {}
        for pm in re.finditer(r"\^([0-9A-Za-z]+)([\^=])([^\^)\s]+)", content):
            col, op, val = pm.group(1).upper(), pm.group(2), pm.group(3)
            if op == "^":
                props[col] = cells.get(val.upper(), val)
            else:
                props[col] = unescape_mork(val)

        subj = props.get("81") or "(No Subject)"
        sender = props.get("82") or "University Sender"
        msg_id = props.get("83")
        recipients_str = props.get("85")
        to_list = [r.strip() for r in recipients_str.split(",")] if recipients_str else []
        date_hex = props.get("86") or props.get("B4")
        ews_id = props.get("B1")
        preview = props.get("B7") or ""

        # Avoid empty or non-email metadata rows (thread tables, folder settings)
        if not subj and not sender and not ews_id:
            continue
        if subj in ("imageSize", "sortColumns") or sender in ("customSortCol", "imageSize"):
            continue
        if subj == "(No Subject)" and sender == "University Sender" and not preview and not date_hex:
            continue

        uid = ews_id or msg_id or f"tb_{row_id}"
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        dt_obj: datetime | None = None
        if date_hex:
            try:
                epoch = int(date_hex, 16)
                if 1000000000 < epoch < 2500000000:
                    dt_obj = datetime.fromtimestamp(epoch, tz=timezone.utc)
            except Exception:
                pass

        messages.append(
            RawMessage(
                uid=uid,
                message_id=msg_id,
                subject=subj,
                from_=sender,
                to=to_list,
                date=dt_obj,
                text_body=preview,
                headers={"source": "thunderbird_profile", "msf_row": row_id},
            )
        )

    # Sort messages by date descending (newest first)
    messages.sort(
        key=lambda x: x.date.timestamp() if x.date else 0,
        reverse=True,
    )

    logger.info(
        "Parsed %d messages from Thunderbird msf file: %s",
        len(messages),
        msf_path.name,
    )
    return messages


class ThunderbirdConnector:
    """Connects to local Mozilla Thunderbird profile to extract university emails."""

    def __init__(
        self,
        settings: EmailMonitorSettings | None = None,
        msf_path: str | Path | None = None,
    ):
        self.settings = settings
        self.msf_path = find_thunderbird_inbox_msf(override_path=msf_path)

    def test_connection(self) -> ConnectionResult:
        """Test whether Thunderbird profile and Inbox.msf file are located and accessible."""
        if not self.msf_path or not self.msf_path.exists():
            return ConnectionResult(
                success=False,
                message="Thunderbird Inbox.msf not found in local profile directory.",
                folder_info=None,
            )

        try:
            msgs = parse_mork_msf(self.msf_path)
            return ConnectionResult(
                success=True,
                message=f"Found Thunderbird Inbox ({len(msgs)} messages discovered).",
                folder_info=str(self.msf_path),
            )
        except Exception as exc:
            return ConnectionResult(
                success=False,
                message=f"Error reading Thunderbird Inbox: {exc}",
                folder_info=str(self.msf_path),
            )

    def list_new_messages(
        self, cursor: MailCursor
    ) -> tuple[list[RawMessage], MailCursor]:
        """Fetch all messages from Thunderbird Inbox.msf, filtered by cursor time if present."""
        if not self.msf_path or not self.msf_path.exists():
            return [], cursor

        all_msgs = parse_mork_msf(self.msf_path)
        new_cursor = MailCursor(
            last_uid=len(all_msgs),
            last_poll_at=datetime.now(timezone.utc).timestamp(),
        )

        return all_msgs, new_cursor

    def fetch_range(
        self, since: datetime, until: datetime, limit: int = 50
    ) -> list[RawMessage]:
        """Return messages within the specified time range."""
        if not self.msf_path or not self.msf_path.exists():
            return []

        all_msgs = parse_mork_msf(self.msf_path)
        matched = []
        for m in all_msgs:
            if m.date and (since <= m.date <= until):
                matched.append(m)
            if len(matched) >= limit:
                break
        return matched
