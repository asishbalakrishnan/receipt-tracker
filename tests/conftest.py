import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # enough for content sniffing; the fake extractor never decodes it


class FakeExtractor:
    """Returns whatever the test sets in .reply (a dict), or raises if .reply is an Exception."""

    name = "fake"

    def __init__(self):
        self.reply = {}
        self.calls = []

    def extract(self, item, categories, examples):
        self.calls.append({"categories": categories, "examples": examples, "text": item.text})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def good_reply(**over):
    reply = {
        "merchant": "Swiggy", "date": "2026-09-20", "total": 349.0, "currency": "INR", "tax_total": 16.62,
        "gstin": "27AAPFU0939F1ZV", "payment_method": "upi", "document_type": "delivery",
        "category": "Food > Food delivery", "kind": "personal",
        "line_items": [
            {"description": "Paneer wrap", "quantity": 2, "amount": 300.0},
            {"description": "Delivery fee", "quantity": 1, "amount": 49.0},
        ],
        "confidence": {"merchant": 0.98, "date": 0.95, "total": 0.99, "tax": 0.9, "gstin": 0.9, "category": 0.9},
        "notes": "",
    }
    reply.update(over)
    return reply


@pytest.fixture
def fake():
    return FakeExtractor()


@pytest.fixture
def client(tmp_path, fake):
    settings = Settings(data_dir=tmp_path, llm_api_key="", token="", sync_ingest=True)
    app = create_app(settings, extractor=fake)
    return TestClient(app)


@pytest.fixture
def upload(client):
    def _upload(name="bill.jpg", data=JPEG):
        return client.post("/api/receipts", files={"files": (name, data, "application/octet-stream")}).json()
    return _upload
