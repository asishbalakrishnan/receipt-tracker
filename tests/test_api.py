import io
import json

from conftest import JPEG, good_reply


def first_txn(resp):
    return resp["results"][0]["transactions"][0]


def test_clean_receipt_is_auto_accepted(client, fake, upload):
    fake.reply = good_reply()
    t = first_txn(upload())
    assert t["status"] == "accepted" and t["flags"] == []
    assert t["amount_paise"] == 34900 and t["tax_amount_paise"] == 1662
    assert t["category"] == "Food > Food delivery" and t["has_file"]
    assert t["field_origin"]["amount_paise"] == "ai"


def test_original_is_encrypted_at_rest_and_served_back(client, fake, upload, tmp_path):
    fake.reply = good_reply()
    t = first_txn(upload(data=JPEG))
    stored = list((tmp_path / "files").glob("*.enc"))
    assert len(stored) == 1 and stored[0].read_bytes() != JPEG and JPEG not in stored[0].read_bytes()
    r = client.get(f"/api/transactions/{t['id']}/file")
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-security-policy"] == "sandbox"


def test_low_confidence_and_bad_gstin_go_to_review(client, fake, upload):
    fake.reply = good_reply(gstin="27AAPFU0939F1ZX", confidence={"merchant": 0.9, "date": 0.5, "total": 0.9, "category": 0.9})
    t = first_txn(upload())
    assert t["status"] == "needs_review"
    assert "gstin_invalid" in t["flags"] and "low_confidence:date" in t["flags"]


def test_extractor_failure_keeps_the_file_for_manual_entry(client, fake, upload):
    fake.reply = RuntimeError("boom")
    t = first_txn(upload())
    assert t["status"] == "needs_review" and t["has_file"]
    assert any(f.startswith("extraction_failed") for f in t["flags"])
    assert "total_missing" in t["flags"]


def test_unsupported_file_type_is_rejected_cleanly(client, upload):
    res = upload("notes.txt", b"hello world")["results"][0]
    assert "Unsupported" in res["error"]


def test_duplicate_detection(client, fake, upload):
    fake.reply = good_reply()
    upload(data=JPEG)
    second = first_txn(upload(data=JPEG + b"1"))          # different bytes, same merchant/date/amount
    assert "possible_duplicate" in second["flags"]
    third = first_txn(upload(data=JPEG + b"1"))           # identical file again
    assert "duplicate_file" in third["flags"]


def test_merchant_rule_beats_model_and_is_learned_from_review(client, fake, upload):
    cats = {c["path"]: c["id"] for c in client.get("/api/categories").json()}
    fake.reply = good_reply(category="Shopping > Clothing", confidence={"merchant": 0.9, "date": 0.9, "total": 0.9, "category": 0.5})
    t = first_txn(upload())
    assert t["status"] == "needs_review" and "low_confidence:category" in t["flags"]

    r = client.patch(f"/api/transactions/{t['id']}", json={"category_id": cats["Food > Food delivery"], "make_rule": True})
    assert r.status_code == 200 and r.json()["status"] == "reviewed" and r.json()["flags"] == []
    assert r.json()["field_origin"]["category_id"] == "manual"

    fake.reply = good_reply(category="Shopping > Clothing", date="2026-09-21", total=100.0, line_items=[])
    t2 = first_txn(upload(data=JPEG + b"2"))
    assert t2["category"] == "Food > Food delivery" and t2["field_origin"]["category_id"] == "rule"
    assert t2["status"] == "accepted"


def test_corrections_feed_back_into_the_next_prompt(client, fake, upload):
    cats = {c["path"]: c["id"] for c in client.get("/api/categories").json()}
    fake.reply = good_reply(confidence={"merchant": 0.9, "date": 0.9, "total": 0.9, "category": 0.4})
    t = first_txn(upload())
    client.patch(f"/api/transactions/{t['id']}", json={"category_id": cats["Shopping > Clothing"]})
    upload(data=JPEG + b"3")
    assert any("Swiggy -> category Shopping > Clothing" in e for e in fake.calls[-1]["examples"])


def test_manual_entry_edit_and_delete(client):
    r = client.post("/api/transactions", json={"merchant": "Chai stall", "amount": "40", "date": "2026-09-22"})
    assert r.status_code == 201
    t = r.json()
    assert t["amount_paise"] == 4000 and t["status"] == "reviewed" and not t["has_file"]
    assert client.patch(f"/api/transactions/{t['id']}", json={"amount": "45.50"}).json()["amount_paise"] == 4550
    assert client.post("/api/transactions", json={"merchant": "x", "amount": -5, "date": "2026-09-22"}).status_code == 422
    assert client.patch(f"/api/transactions/{t['id']}", json={"date": "22/09/2026"}).status_code == 422
    assert client.delete(f"/api/transactions/{t['id']}").status_code == 204
    assert client.get(f"/api/transactions/{t['id']}").status_code == 404


def test_delete_removes_the_encrypted_file(client, fake, upload, tmp_path):
    fake.reply = good_reply()
    t = first_txn(upload())
    client.delete(f"/api/transactions/{t['id']}")
    assert list((tmp_path / "files").glob("*.enc")) == []


def test_email_upload_creates_transaction_from_text(client, fake):
    fake.reply = good_reply(merchant="Uber")
    eml = (b"From: noreply@uber.com\nSubject: Your trip\nDate: Mon, 21 Sep 2026 10:00:00 +0530\n"
           b"Content-Type: text/plain\n\nTotal: Rs 212.40")
    res = client.post("/api/receipts", files={"files": ("trip.eml", eml, "message/rfc822")}).json()
    t = res["results"][0]["transactions"][0]
    assert t["source"] == "email" and "Total: Rs 212.40" in fake.calls[-1]["text"]


def test_summary_and_month_filter(client, fake, upload):
    fake.reply = good_reply()
    upload()
    fake.reply = good_reply(merchant="Uber", category="Transport > Cab and auto", total=200.0,
                            tax_total=None, gstin=None, line_items=[], date="2026-09-25")
    upload(data=JPEG + b"9")
    s = client.get("/api/summary", params={"month": "2026-09"}).json()
    assert s["total_paise"] == 34900 + 20000 and s["count"] == 2
    assert {c["category"] for c in s["by_category"]} == {"Food", "Transport"}
    assert len(client.get("/api/transactions", params={"month": "2026-08"}).json()) == 0
    assert client.get("/api/summary", params={"month": "bad"}).status_code == 422


def test_csv_export_neutralises_formula_injection(client):
    client.post("/api/transactions", json={"merchant": "=HYPERLINK(\"http://evil\")", "amount": 10, "date": "2026-09-22"})
    body = client.get("/api/export.csv", params={"month": "2026-09"}).text
    assert "'=HYPERLINK" in body and "\n=HYPERLINK" not in body


def test_categories_and_rules_api(client):
    cid = client.post("/api/categories", json={"name": "Pets"}).json()["id"]
    assert client.post("/api/categories", json={"name": "Pets"}).status_code == 409
    child = client.post("/api/categories", json={"name": "Vet", "parent_id": cid}).json()["id"]
    assert client.post("/api/categories", json={"name": "Deep", "parent_id": child}).status_code == 422
    assert client.post("/api/rules", json={"merchant": "Vet Clinic", "category_id": child}).status_code == 201
    assert client.get("/api/rules").json()[0]["category"] == "Pets > Vet"
    assert client.post("/api/rules", json={"merchant": "x"}).status_code == 422


def test_export_all_and_wipe(client):
    client.post("/api/transactions", json={"merchant": "A", "amount": 10, "date": "2026-09-22"})
    data = json.loads(client.get("/api/export.json").text)
    assert len(data["transactions"]) == 1
    assert client.post("/api/transactions", json={"merchant": "B", "amount": 5, "date": "2026-09-22"}).status_code == 201
    assert client.post("/api/data/wipe", params={"confirm": "no"}).status_code == 422
    assert client.post("/api/data/wipe", params={"confirm": "DELETE"}).json()["deleted_transactions"] == 2
    assert client.get("/api/transactions").json() == []


def test_token_auth(tmp_path, fake):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    c = TestClient(create_app(Settings(data_dir=tmp_path, token="s3cret"), extractor=fake))
    assert c.get("/api/transactions").status_code == 401
    assert c.get("/api/transactions", headers={"x-token": "wrong"}).status_code == 401
    assert c.get("/api/transactions", headers={"x-token": "s3cret"}).status_code == 200
    assert c.get("/api/health").status_code == 200 and c.get("/").status_code in (200, 404)
