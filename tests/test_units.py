from datetime import date

from app.classify import clean_merchant, merchant_key
from app.emailparse import html_to_text, parse_eml
from app.extractor import parse_json_object
from app.money import fmt_inr, to_paise
from app.validate import gstin_valid, validate


def test_to_paise_handles_indian_formats():
    assert to_paise("1,23,456.50") == 12345650
    assert to_paise("₹249") == 24900
    assert to_paise(249.5) == 24950
    assert to_paise(0.1 + 0.2) == 30  # no float drift
    assert to_paise("abc") is None and to_paise(None) is None and to_paise(True) is None


def test_fmt_inr_grouping():
    assert fmt_inr(12345650) == "Rs 1,23,456.50"
    assert fmt_inr(99) == "Rs 0.99"
    assert fmt_inr(10000000) == "Rs 1,00,000.00"


def test_gstin():
    assert gstin_valid("27AAPFU0939F1ZV")
    assert not gstin_valid("27AAPFU0939F1ZX")  # bad checksum
    assert not gstin_valid("NOTAGSTIN")
    assert not gstin_valid(None)


def base(**over):
    rec = {"merchant": "X", "date": "2026-09-20", "amount_paise": 10000, "currency": "INR",
           "tax_amount_paise": None, "gstin": None, "line_items": []}
    rec.update(over)
    return rec


def test_validate_clean_record_has_no_flags():
    assert validate(base(), {"merchant": 0.9, "date": 0.9, "total": 0.9}, 0.8, date(2026, 9, 29)) == []


def test_validate_flags():
    today = date(2026, 9, 29)
    assert "total_missing" in validate(base(amount_paise=None), {}, 0.8, today)
    assert "date_in_future" in validate(base(date="2026-12-01"), {}, 0.8, today)
    assert "date_missing" in validate(base(date=None), {}, 0.8, today)
    assert "non_inr_currency" in validate(base(currency="USD"), {}, 0.8, today)
    assert "gstin_invalid" in validate(base(gstin="27AAPFU0939F1ZX"), {}, 0.8, today)
    assert "low_confidence:total" in validate(base(), {"total": 0.4}, 0.8, today)


def test_line_items_sum_check():
    today = date(2026, 9, 29)
    ok = base(amount_paise=34900, line_items=[{"amount_paise": 30000}, {"amount_paise": 4900}])
    assert "line_items_mismatch" not in validate(ok, {}, 0.8, today)
    tax_split = base(amount_paise=10500, tax_amount_paise=500, line_items=[{"amount_paise": 10000}])
    assert "line_items_mismatch" not in validate(tax_split, {}, 0.8, today)
    bad = base(amount_paise=34900, line_items=[{"amount_paise": 10000}])
    assert "line_items_mismatch" in validate(bad, {}, 0.8, today)


def test_parse_json_object_tolerates_fences_and_chatter():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": "}"}} done') == {"a": {"b": "}"}}


def test_merchant_helpers():
    assert merchant_key("SWIGGY  Ltd.") == "swiggyltd"
    assert clean_merchant("  Big   Bazaar. ") == "Big Bazaar"


def test_email_parsing_text_and_html():
    raw = (b"From: orders@swiggy.in\nSubject: Your order\nDate: Mon, 21 Sep 2026 10:00:00 +0530\n"
           b"Content-Type: text/html\n\n<html><style>p{}</style><p>Total <b>Rs 349</b></p><script>x()</script></html>")
    parts = parse_eml(raw)
    assert len(parts) == 1 and parts[0].data is None
    assert "Total Rs 349" in parts[0].text and "x()" not in parts[0].text and "Swiggy" not in parts[0].text.replace("swiggy", "")
    assert html_to_text("<div>a</div><div>b</div>") == "a\nb"


# --- OpenAI-compatible extractor -------------------------------------------------------------
import json as _json

import httpx

from app.extractor import ExtractInput, OpenAICompatExtractor, build_extractor
from app.config import Settings


def _extractor(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatExtractor("http://llm.local/v1/", "k", "some-model", client=client, **kw)


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_openai_extractor_request_shape_and_reply_parsing():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], _json.loads(req.content)
        return _ok('```json\n{"merchant": "Swiggy", "total": 10}\n```')

    out = _extractor(handler).extract(ExtractInput(b"\xff\xd8\xff\xe0" + b"0" * 32, "image/jpeg"), ["Food"], ["a -> b"])
    assert out == {"merchant": "Swiggy", "total": 10}
    assert seen["url"] == "http://llm.local/v1/chat/completions" and seen["auth"] == "Bearer k"
    body = seen["body"]
    assert body["model"] == "some-model" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    kinds = [p["type"] for p in body["messages"][1]["content"]]
    assert kinds == ["image_url", "text"]
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "a -> b" in body["messages"][1]["content"][1]["text"]


def test_openai_extractor_retries_without_response_format_and_sends_pdf_natively():
    calls = []

    def handler(req):
        body = _json.loads(req.content)
        calls.append(body)
        return httpx.Response(400, text="unsupported") if "response_format" in body else _ok('{"merchant": "X"}')

    out = _extractor(handler, pdf_mode="file").extract(ExtractInput(b"%PDF-1.4", "application/pdf"), ["Food"], [])
    assert out == {"merchant": "X"} and len(calls) == 2 and "response_format" not in calls[1]
    part = calls[1]["messages"][1]["content"][0]
    assert part["type"] == "file" and part["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_openai_extractor_surfaces_api_errors_and_bad_shapes():
    import pytest
    with pytest.raises(RuntimeError, match="401"):
        _extractor(lambda r: httpx.Response(401, text="nope")).extract(ExtractInput(None, None, "hi"), ["Food"], [])
    with pytest.raises(RuntimeError, match="unexpected"):
        _extractor(lambda r: httpx.Response(200, json={"oops": 1})).extract(ExtractInput(None, None, "hi"), ["Food"], [])


def test_build_extractor_selection(monkeypatch):
    for v in ("RT_LLM_API_KEY", "OPENAI_API_KEY", "RT_LLM_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    assert build_extractor(Settings()).name == "none"
    monkeypatch.setenv("RT_LLM_BASE_URL", "http://localhost:11434/v1")     # keyless local server
    assert build_extractor(Settings()).name == "openai-compatible"
    monkeypatch.delenv("RT_LLM_BASE_URL")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert build_extractor(Settings()).name == "openai-compatible"
