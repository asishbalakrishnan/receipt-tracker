"""Email intake: check a dedicated mailbox over IMAP and turn forwarded receipts into transactions.

Safety rules, because anyone who learns the address can send it mail:
  * Only senders in RT_MAIL_ALLOWED_SENDERS are processed.
  * The From header alone proves nothing (it is trivially forged), so by default the receiving provider's
    own Authentication-Results header must show dmarc=pass. Everything else goes to the Rejected folder and is
    never shown to the model.
  * Oversized messages are rejected.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import threading
from email import policy
from email.utils import getaddresses
from typing import Protocol

from . import db, service

log = logging.getLogger("receipts.mail")


class Mailbox(Protocol):
    def fetch_unseen(self) -> list[tuple[str, bytes]]: ...
    def move(self, uid: str, folder: str) -> None: ...
    def close(self) -> None: ...


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, folder: str = "INBOX", timeout: int = 30):
        self._m = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        self._m.login(user, password)
        typ, _ = self._m.select(folder)
        if typ != "OK":
            raise RuntimeError(f"cannot open mailbox folder {folder!r}")

    def fetch_unseen(self) -> list[tuple[str, bytes]]:
        typ, data = self._m.uid("SEARCH", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        out = []
        for uid in data[0].split():
            typ, parts = self._m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ == "OK" and parts and isinstance(parts[0], tuple):
                out.append((uid.decode(), parts[0][1]))
        return out

    def move(self, uid: str, folder: str) -> None:
        try:
            self._m.create(folder)          # already existing is fine
        except Exception:
            pass
        typ, _ = self._m.uid("COPY", uid, folder)
        if typ != "OK":
            raise RuntimeError(f"could not copy message to {folder!r}")
        self._m.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        self._m.expunge()

    def close(self) -> None:
        try:
            self._m.logout()
        except Exception:
            pass


def _sender_addresses(msg) -> set[str]:
    return {addr.lower() for _, addr in getaddresses(msg.get_all("from", [])) if addr}


def _dmarc_pass(msg) -> bool:
    return any(re.search(r"\bdmarc=pass\b", h, re.I) for h in msg.get_all("authentication-results", []))


def check_message(raw: bytes, allowed: set[str], require_auth: bool) -> str | None:
    """Return None if the message may be processed, else the reason it is rejected."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    senders = _sender_addresses(msg)
    if not senders & allowed:
        return "sender not allowed"
    if len(senders) != 1:
        return "ambiguous sender"
    if require_auth and not _dmarc_pass(msg):
        return "sender could not be verified (no dmarc=pass)"
    return None


def poll_once(settings, vault, mailbox: Mailbox) -> dict:
    """Process every unread message in the mailbox. Returns counts for logging."""
    stats = {"queued": 0, "rejected": 0, "unusable": 0}
    conn = db.connect(settings.db_path)
    try:
        for uid, raw in mailbox.fetch_unseen():
            if len(raw) > settings.max_upload_bytes:
                reason = "message too large"
            else:
                reason = check_message(raw, settings.allowed_senders, settings.mail_require_auth)
            if reason:
                log.warning("rejected email %s: %s", uid, reason)
                mailbox.move(uid, settings.imap_reject_folder)
                stats["rejected"] += 1
                continue
            try:
                txns = service.ingest_file(conn, settings, vault, None, raw, "email.eml", defer=True)
                stats["queued"] += len(txns)
                mailbox.move(uid, settings.imap_done_folder)
            except ValueError as exc:           # no usable attachment or text
                log.warning("unusable email %s: %s", uid, exc)
                mailbox.move(uid, settings.imap_reject_folder)
                stats["unusable"] += 1
    finally:
        conn.close()
    return stats


class MailThread:
    """Polls the mailbox on a timer. Errors (network, bad password) are logged and retried."""

    def __init__(self, settings, vault, worker, factory=None):
        self.settings, self.vault, self.worker = settings, vault, worker
        self._factory = factory or (lambda: ImapMailbox(
            settings.imap_host, settings.imap_port, settings.imap_user, settings.imap_password, settings.imap_folder))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="mail-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_once(self) -> dict | None:
        try:
            mailbox = self._factory()
            try:
                stats = poll_once(self.settings, self.vault, mailbox)
            finally:
                mailbox.close()
            self.last_error = None
            if stats["queued"]:
                self.worker.wake()
            return stats
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.error("mail poll failed: %s", exc)
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(max(30, self.settings.mail_poll_seconds))
