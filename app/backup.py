"""Backups: an encrypted database snapshot plus a mirror of the (already encrypted) receipt files.

  python -m app.backup /path/to/backup-dir             take a backup
  python -m app.backup restore /path/to/backup-dir     restore into RT_DATA_DIR (needs the same RT_KEY)

The database snapshot is encrypted with the same key as the receipt files, so a copy of the backup directory
is safe to put in cloud storage, and restoring needs only RT_KEY. Keep RT_KEY somewhere that is NOT the server
(your password manager). Receipt files never change once written, so only new ones are copied; files you have
deleted in the app are removed from the mirror on the next run (your right to erasure covers backups too).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

from .config import Settings
from .crypto import load_key

KEEP_SNAPSHOTS = 14


def _snapshot_db(db_path: Path) -> bytes:
    """A consistent copy of a live SQLite database (safe while the app is writing)."""
    tmp = db_path.with_suffix(".snapshot")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def make_backup(settings: Settings, dest: Path, now: datetime | None = None, keep: int = KEEP_SNAPSHOTS) -> dict:
    key = load_key(settings.data_dir, settings.fernet_key)
    now = now or datetime.now()
    (dest / "db").mkdir(parents=True, exist_ok=True)
    (dest / "files").mkdir(parents=True, exist_ok=True)

    name = f"receipts-{now:%Y%m%d-%H%M%S}.db.enc"
    (dest / "db" / name).write_bytes(Fernet(key).encrypt(_snapshot_db(settings.db_path)))
    for old in sorted((dest / "db").glob("receipts-*.db.enc"))[:-keep]:
        old.unlink()

    live = {p.name for p in settings.files_dir.glob("*.enc")}
    copied = 0
    for fname in live:
        target = dest / "files" / fname
        if not target.exists():
            shutil.copy2(settings.files_dir / fname, target)
            copied += 1
    removed = 0
    for p in (dest / "files").glob("*.enc"):
        if p.name not in live:
            p.unlink()
            removed += 1
    return {"snapshot": name, "files_copied": copied, "files_removed": removed, "files_total": len(live)}


def restore_backup(settings: Settings, src: Path, force: bool = False) -> dict:
    key = load_key(settings.data_dir, settings.fernet_key)
    snaps = sorted((src / "db").glob("receipts-*.db.enc"))
    if not snaps:
        raise SystemExit("No database snapshot found in that backup.")
    if settings.db_path.exists() and not force:
        raise SystemExit(f"{settings.db_path} already exists. Move it away, or pass --force to overwrite.")
    try:
        data = Fernet(key).decrypt(snaps[-1].read_bytes())
    except Exception:
        raise SystemExit("Could not decrypt the snapshot: RT_KEY is not the key this backup was made with.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes(data)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in (src / "files").glob("*.enc"):
        shutil.copy2(p, settings.files_dir / p.name)
        n += 1
    return {"snapshot": snaps[-1].name, "files_restored": n}


def main(argv: list[str]) -> None:  # pragma: no cover
    settings = Settings()
    if argv and argv[0] == "restore":
        args = [a for a in argv[1:] if a != "--force"]
        print(restore_backup(settings, Path(args[0]), force="--force" in argv))
    elif argv:
        print(make_backup(settings, Path(argv[0])))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
