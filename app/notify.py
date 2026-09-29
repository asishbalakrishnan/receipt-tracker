"""Optional phone notifications through ntfy (https://ntfy.sh, or your own server).

Messages are batched (a burst of receipts becomes one notification) and contain only counts unless
RT_NTFY_DETAILS=1, because a public ntfy topic is not private.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("receipts.notify")


class Notifier:
    def __init__(self, url: str, token: str = "", details: bool = False, click_url: str = "",
                 delay: float = 20.0, post=None):
        self.url, self.token, self.details, self.click_url, self.delay = url, token, details, click_url, delay
        self._post = post or self._http_post
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending: list[dict] = []

    def __call__(self, txn: dict) -> None:
        with self._lock:
            self._pending.append(txn)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        with self._lock:
            batch, self._pending, self._timer = self._pending, [], None
        if not batch:
            return
        review = [t for t in batch if t.get("status") == "needs_review"]
        filed = len(batch) - len(review)
        parts = []
        if filed:
            parts.append(f"{filed} filed")
        if review:
            parts.append(f"{len(review)} need review")
        body = ", ".join(parts)
        if self.details:
            lines = [f"{t.get('merchant') or 'Unknown'} {(t['amount_paise'] or 0) / 100:.2f}" for t in batch[:5]]
            body += "\n" + "\n".join(lines)
        headers = {"Title": "Receipts", "Tags": "receipt", "Priority": "high" if review else "default"}
        if self.click_url:
            headers["Click"] = self.click_url
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            self._post(self.url, body, headers)
        except Exception:
            log.exception("could not send notification")

    @staticmethod
    def _http_post(url: str, body: str, headers: dict) -> None:
        import httpx
        httpx.post(url, content=body.encode(), headers=headers, timeout=10).raise_for_status()


def build_notifier(settings) -> Notifier | None:
    return Notifier(settings.ntfy_url, settings.ntfy_token, settings.ntfy_details, settings.public_url) if settings.ntfy_url else None
