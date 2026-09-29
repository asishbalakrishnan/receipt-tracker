"""Turn a forwarded .eml into receipt inputs: PDF/image attachments, else the message text."""
from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MIN_IMAGE_BYTES = 20_000  # skip logos and tracking pixels


@dataclass
class EmailPart:
    data: bytes | None
    media_type: str | None
    text: str | None
    name: str


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.out)
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def parse_eml(raw: bytes) -> list[EmailPart]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    header = f"From: {msg.get('From', '')}\nSubject: {msg.get('Subject', '')}\nDate: {msg.get('Date', '')}\n"

    pdfs: list[EmailPart] = []
    images: list[EmailPart] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart() or not (ctype == "application/pdf" or ctype in IMAGE_TYPES):
            continue
        payload = part.get_payload(decode=True) or b""
        name = part.get_filename() or ("attachment.pdf" if ctype == "application/pdf" else "image")
        if ctype == "application/pdf":
            pdfs.append(EmailPart(payload, ctype, header, name))
        elif len(payload) >= MIN_IMAGE_BYTES:
            images.append(EmailPart(payload, ctype, header, name))
    if pdfs:
        return pdfs
    if images:
        return images

    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_type() == "text/html" else content
    if not text.strip():
        return []
    return [EmailPart(None, None, header + "\n" + text, msg.get("Subject", "email") or "email")]
