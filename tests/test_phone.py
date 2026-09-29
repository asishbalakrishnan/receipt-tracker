"""Login, background processing and multi-page capture."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from app import auth
from app.config import Settings
from app.main import create_app
from conftest import JPEG, good_reply

PASSWORD = "correct horse battery"
HDR = {"x-requested-with": "receipt-tracker"}


def make_client(tmp_path, fake, **kw):
    kw.setdefault("token", "")
    return TestClient(create_app(Settings(data_dir=tmp_path, llm_api_key="", **kw), extractor=fake))


def png(color, size=(200, 120)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---- login ------------------------------------------------------------------------------------

def login_client(tmp_path, fake, token=""):
    h = auth.hash_password(PASSWORD, n=2**4)
    return make_client(tmp_path, fake, password_hash=h, token=token, sync_ingest=True)


def test_password_login_flow(tmp_path, fake):
    c = login_client(tmp_path, fake)
    assert c.get("/api/session").json() == {"mode": "password", "authenticated": False}
    assert c.get("/api/transactions").status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    r = c.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/session").json()["authenticated"] is True
    assert c.get("/api/transactions").status_code == 200
    c.post("/api/logout")
    assert c.get("/api/transactions").status_code == 401


def test_cookie_writes_need_the_csrf_header(tmp_path, fake):
    c = login_client(tmp_path, fake)
    c.post("/api/login", json={"password": PASSWORD})
    body = {"merchant": "Chai", "amount": 40, "date": "2026-09-22"}
    assert c.post("/api/transactions", json=body).status_code == 403
    assert c.post("/api/transactions", json=body, headers=HDR).status_code == 201


def test_api_token_still_works_for_scripts_without_cookie_or_csrf_header(tmp_path, fake):
    c = login_client(tmp_path, fake, token="s3cret")
    body = {"merchant": "Chai", "amount": 40, "date": "2026-09-22"}
    assert c.post("/api/transactions", json=body).status_code == 401
    assert c.post("/api/transactions", json=body, headers={"authorization": "Bearer s3cret"}).status_code == 201
    assert c.get("/api/transactions", headers={"x-token": "s3cret"}).status_code == 200
    assert c.get("/api/transactions", headers={"x-token": "wrong"}).status_code == 401


def test_login_is_rate_limited(tmp_path, fake):
    c = login_client(tmp_path, fake)
    for _ in range(5):
        assert c.post("/api/login", json={"password": "bad"}).status_code == 401
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_changing_the_password_invalidates_old_sessions(tmp_path, fake):
    c = login_client(tmp_path, fake)
    c.post("/api/login", json={"password": PASSWORD})
    cookie = c.cookies.get("rt_session")
    other = make_client(tmp_path, fake, password_hash=auth.hash_password("another long passphrase", n=2**4),
                        sync_ingest=True)
    other.cookies.set("rt_session", cookie)
    assert other.get("/api/transactions").status_code == 401


def test_password_hash_roundtrip():
    h = auth.hash_password("abc", n=2**4)
    assert auth.verify_password("abc", h) and not auth.verify_password("abd", h)
    assert not auth.verify_password("abc", "garbage")


# ---- background processing ----------------------------------------------------------------------

def test_upload_returns_immediately_and_worker_reads_it_later(tmp_path, fake):
    fake.reply = good_reply()
    c = make_client(tmp_path, fake)                      # sync_ingest off: production behaviour
    res = c.post("/api/receipts", files={"files": ("a.jpg", JPEG, "image/jpeg")}).json()
    t = res["results"][0]["transactions"][0]
    assert t["status"] == "processing" and t["has_file"] and t["amount_paise"] is None and fake.calls == []
    assert c.app.state.worker.drain() == 1
    t = c.get(f"/api/transactions/{t['id']}").json()
    assert t["status"] == "accepted" and t["amount_paise"] == 34900 and t["merchant"] == "Swiggy"


def test_processing_survives_a_restart(tmp_path, fake):
    fake.reply = good_reply()
    make_client(tmp_path, fake).post("/api/receipts", files={"files": ("a.jpg", JPEG, "image/jpeg")})
    again = make_client(tmp_path, fake)                  # new process, same data dir
    assert again.app.state.worker.drain() == 1
    assert again.get("/api/transactions").json()[0]["status"] == "accepted"


def test_worker_handles_model_failure_and_duplicates_in_queue(tmp_path, fake):
    c = make_client(tmp_path, fake)
    fake.reply = RuntimeError("model down")
    c.post("/api/receipts", files={"files": ("a.jpg", JPEG, "image/jpeg")})
    c.app.state.worker.drain()
    t = c.get("/api/transactions").json()[0]
    assert t["status"] == "needs_review" and t["has_file"] and t["flags"][0].startswith("extraction_failed")

    fake.reply = good_reply()
    for i in range(2):                                   # same content twice, queued together
        c.post("/api/receipts", files={"files": ("b.jpg", JPEG + b"1", "image/jpeg")})
    c.app.state.worker.drain()
    flags = [tuple(x["flags"]) for x in c.get("/api/transactions").json() if x["merchant"] == "Swiggy"]
    assert sorted(flags) == [(), ("duplicate_file", "possible_duplicate")] or "duplicate_file" in flags[0] + flags[1]


def test_email_attachment_keeps_headers_as_context_for_the_model(tmp_path, fake):
    fake.reply = good_reply()
    c = make_client(tmp_path, fake)
    eml = (b"From: a@b.c\nSubject: Invoice\nDate: Mon, 21 Sep 2026 10:00:00 +0530\nMIME-Version: 1.0\n"
           b"Content-Type: multipart/mixed; boundary=X\n\n--X\nContent-Type: text/plain\n\nhi\n--X\n"
           b"Content-Type: application/pdf; name=i.pdf\nContent-Transfer-Encoding: base64\n\nJVBERi0xLjQK\n--X--\n")
    c.post("/api/receipts", files={"files": ("m.eml", eml, "message/rfc822")})
    c.app.state.worker.drain()
    assert "Subject: Invoice" in fake.calls[-1]["text"]


# ---- multi-page capture --------------------------------------------------------------------------

def test_combine_stitches_pages_into_one_receipt(client, fake):
    fake.reply = good_reply()
    files = [("files", ("p1.png", png("white"), "image/png")), ("files", ("p2.png", png("gray", (300, 100)), "image/png"))]
    res = client.post("/api/receipts", params={"combine": "true"}, files=files).json()["results"]
    assert len(res) == 1 and res[0]["transactions"][0]["receipt_name"] == "2-page receipt.jpg"
    stored = client.get(f"/api/transactions/{res[0]['transactions'][0]['id']}/file").content
    img = Image.open(io.BytesIO(stored))
    assert img.height > 200 and img.format == "JPEG"


def test_without_combine_each_photo_is_its_own_receipt(client, fake):
    fake.reply = good_reply()
    files = [("files", ("p1.png", png("white"), "image/png")), ("files", ("p2.png", png("gray"), "image/png"))]
    assert len(client.post("/api/receipts", files=files).json()["results"]) == 2


def test_pwa_files_are_public(client):
    assert client.get("/sw.js").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/icons/../app.py").status_code in (404, 422)


def test_require_auth_refuses_to_start_open(tmp_path, fake):
    import pytest
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app(Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", require_auth=True), extractor=fake)
    create_app(Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash=auth.hash_password("x", n=2**4),
                        require_auth=True), extractor=fake)
