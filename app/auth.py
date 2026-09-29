"""Single-user login: scrypt password hash, signed session cookie, and a login rate limiter.

Generate a hash for RT_PASSWORD_HASH with:  python -m app.auth
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import deque

COOKIE = "rt_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "receipt-tracker"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * n * r * 2)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=n, r=r, p=p,
                            maxmem=128 * n * r * 2)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except Exception:
        return False


class Sessions:
    """Stateless signed cookies. Changing the password (or key) invalidates every session."""

    def __init__(self, secret: bytes, days: int = 30):
        self._secret = secret
        self.ttl = days * 86400

    def _sig(self, body: str) -> str:
        return hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        body = f"{int(time.time()) + self.ttl}.{secrets.token_hex(8)}"
        return f"{body}.{self._sig(body)}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        try:
            expiry, nonce, sig = cookie.split(".")
            return hmac.compare_digest(sig, self._sig(f"{expiry}.{nonce}")) and int(expiry) > time.time()
        except Exception:
            return False


class LoginLimiter:
    """Allow `limit` failed logins per `window` seconds per client, and 5x that across all clients."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self._by_key: dict[str, deque] = {}
        self._all: deque = deque()

    def _prune(self, q: deque, now: float) -> None:
        while q and q[0] < now - self.window:
            q.popleft()

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._by_key.setdefault(key, deque())
        self._prune(q, now)
        self._prune(self._all, now)
        return len(q) >= self.limit or len(self._all) >= self.limit * 5

    def fail(self, key: str, now: float | None = None) -> None:
        now = now or time.time()
        self._by_key.setdefault(key, deque()).append(now)
        self._all.append(now)

    def reset(self, key: str) -> None:
        self._by_key.pop(key, None)


if __name__ == "__main__":  # pragma: no cover
    import getpass

    pw = getpass.getpass("New password (12+ characters): ")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters.")
    if pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords differ.")
    print("\nPut this in your environment as RT_PASSWORD_HASH (single quotes keep the $ signs safe):\n")
    print(f"RT_PASSWORD_HASH='{hash_password(pw)}'")
