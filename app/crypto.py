"""Encrypted storage for original receipt files (Fernet, AES-128-CBC + HMAC)."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


def load_key(data_dir: Path, env_key: str = "") -> bytes:
    if env_key:
        return env_key.encode()
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / "secret.key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Vault:
    """Writes encrypted blobs under files_dir and reads them back by name."""

    def __init__(self, files_dir: Path, key: bytes):
        self.dir = files_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(key)

    def save(self, data: bytes) -> str:
        name = uuid.uuid4().hex + ".enc"
        (self.dir / name).write_bytes(self._fernet.encrypt(data))
        return name

    def load(self, name: str) -> bytes:
        return self._fernet.decrypt(self._safe(name).read_bytes())

    def delete(self, name: str) -> None:
        try:
            self._safe(name).unlink()
        except FileNotFoundError:
            pass

    def _safe(self, name: str) -> Path:
        path = (self.dir / name).resolve()
        if path.parent != self.dir.resolve():
            raise ValueError("invalid file name")
        return path
