"""Background worker: reads receipts that are waiting in status 'processing'.

The database is the queue, so nothing is lost if the server restarts: on startup any row still
'processing' is simply picked up again.
"""
from __future__ import annotations

import json
import logging
import threading

from . import db, service

log = logging.getLogger("receipts.worker")


class Worker:
    def __init__(self, settings, vault, extractor, notify=None):
        self.settings, self.vault, self.extractor, self.notify = settings, vault, extractor, notify
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="receipt-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        """Process the oldest waiting receipt. Returns False when the queue is empty."""
        conn = db.connect(self.settings.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM transactions WHERE status = 'processing' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            try:
                txn = service.process_txn(conn, self.settings, self.vault, self.extractor, row["id"])
            except Exception as exc:  # e.g. the stored file cannot be decrypted: park it for a human
                log.exception("processing %s failed", row["id"])
                conn.execute(
                    "UPDATE transactions SET status = 'needs_review', flags = ?, updated_at = ? WHERE id = ?",
                    (json.dumps([f"processing_failed: {str(exc)[:80]}"]), service.now(), row["id"]),
                )
                conn.commit()
                txn = service.get_txn(conn, row["id"])
            if self.notify and txn:
                self._notify(conn, txn)
            return True
        finally:
            conn.close()

    def _notify(self, conn, txn: dict) -> None:
        try:
            self.notify(txn)
        except Exception:
            log.exception("notification failed")

    def drain(self) -> int:
        n = 0
        while self.run_once():
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                self._wake.wait(timeout=5)
                self._wake.clear()
