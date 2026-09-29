import sqlite3
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from app import backup
from app.config import Settings
from app.main import create_app
from app.notify import Notifier
from fastapi.testclient import TestClient
from conftest import JPEG, good_reply


def app_with_data(tmp_path, fake, n=2):
    fake.reply = good_reply()
    s = Settings(data_dir=tmp_path / "live", llm_api_key="", token="", password_hash="", sync_ingest=True)
    c = TestClient(create_app(s, extractor=fake))
    for i in range(n):
        c.post("/api/receipts", files={"files": (f"r{i}.jpg", JPEG + bytes([i]), "image/jpeg")})
    return s, c


def test_backup_is_encrypted_mirrors_files_and_restores(tmp_path, fake):
    s, c = app_with_data(tmp_path, fake)
    dest = tmp_path / "bk"
    r = backup.make_backup(s, dest, datetime(2026, 9, 29, 3, 0))
    assert r["files_copied"] == 2 and r["files_total"] == 2
    snap = next((dest / "db").iterdir())
    assert b"SQLite format" not in snap.read_bytes() and b"Swiggy" not in snap.read_bytes()   # the database is encrypted
    assert not list(dest.rglob("secret.key"))                                                # the key is never in a backup

    # second run copies nothing new; deleting a receipt removes its file from the mirror
    assert backup.make_backup(s, dest, datetime(2026, 9, 30, 3, 0))["files_copied"] == 0
    txn = c.get("/api/transactions").json()[0]["id"]
    c.delete(f"/api/transactions/{txn}")
    assert backup.make_backup(s, dest, datetime(2026, 10, 1, 3, 0))["files_removed"] == 1

    # restore into a fresh machine that has only the key
    fresh = Settings(data_dir=tmp_path / "new", fernet_key=(tmp_path / "live" / "secret.key").read_text().strip())
    assert backup.restore_backup(fresh, dest)["files_restored"] == 1
    assert sqlite3.connect(fresh.db_path).execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1   # the latest snapshot, after the delete
    restored = TestClient(create_app(Settings(data_dir=tmp_path / "new", fernet_key=fresh.fernet_key, llm_api_key="",
                                              token="", password_hash=""), extractor=fake))
    left = restored.get("/api/transactions").json()
    assert len(left) == 1 and restored.get(f"/api/transactions/{left[0]['id']}/file").content.startswith(b"\xff\xd8")


def test_restore_refuses_wrong_key_and_existing_data(tmp_path, fake):
    s, _ = app_with_data(tmp_path, fake, n=1)
    dest = tmp_path / "bk"
    backup.make_backup(s, dest)
    wrong = Settings(data_dir=tmp_path / "x", fernet_key=Fernet.generate_key().decode())
    with pytest.raises(SystemExit, match="RT_KEY"):
        backup.restore_backup(wrong, dest)
    with pytest.raises(SystemExit, match="already exists"):
        backup.restore_backup(s, dest)


def test_snapshots_are_pruned(tmp_path, fake):
    s, _ = app_with_data(tmp_path, fake, n=1)
    for day in range(1, 6):
        backup.make_backup(s, tmp_path / "bk", datetime(2026, 9, day), keep=3)
    assert len(list((tmp_path / "bk" / "db").iterdir())) == 3


def test_notifier_batches_and_hides_details_by_default():
    sent = []
    n = Notifier("https://ntfy.example/topic", details=False, click_url="https://r.example", delay=999,
                 post=lambda url, body, headers: sent.append((body, headers)))
    n({"status": "accepted", "merchant": "Swiggy", "amount_paise": 34900})
    n({"status": "needs_review", "merchant": "X", "amount_paise": None})
    n({"status": "accepted", "merchant": "Uber", "amount_paise": 20000})
    n.flush()
    assert len(sent) == 1
    body, headers = sent[0]
    assert body == "2 filed, 1 need review" and "Swiggy" not in body
    assert headers["Click"] == "https://r.example" and headers["Priority"] == "high"
    n.flush()
    assert len(sent) == 1                                     # nothing pending: nothing sent


def test_notifier_details_and_failure_isolation():
    sent = []
    n = Notifier("u", details=True, delay=999, post=lambda *a: sent.append(a))
    n({"status": "accepted", "merchant": "Swiggy", "amount_paise": 34900})
    n.flush()
    assert "Swiggy 349.00" in sent[0][1]

    def boom(*a):
        raise RuntimeError("network")
    bad = Notifier("u", delay=999, post=boom)
    bad({"status": "accepted"})
    bad.flush()                                               # must not raise


def test_worker_calls_notify_after_each_receipt(tmp_path, fake):
    fake.reply = good_reply()
    got = []
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="")
    c = TestClient(create_app(s, extractor=fake, notify=got.append))
    c.post("/api/receipts", files={"files": ("a.jpg", JPEG, "image/jpeg")})
    c.app.state.worker.drain()
    assert len(got) == 1 and got[0]["status"] == "accepted"
