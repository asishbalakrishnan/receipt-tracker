import imaplib

from fastapi.testclient import TestClient

from app import mailbox
from app.config import Settings
from app.main import create_app
from conftest import good_reply

ME = "me@example.com"


def mail(sender=ME, auth=True, subject="Your order", body="Total: Rs 212.40", extra=""):
    headers = f"From: {sender}\nSubject: {subject}\nDate: Mon, 21 Sep 2026 10:00:00 +0530\n"
    if auth:
        headers += "Authentication-Results: mx.example.net; dkim=pass; spf=pass; dmarc=pass header.from=example.com\n"
    return (headers + extra + "Content-Type: text/plain\n\n" + body).encode()


class FakeBox:
    def __init__(self, messages):
        self.unseen = dict(messages)
        self.moved = {}

    def fetch_unseen(self):
        return list(self.unseen.items())

    def move(self, uid, folder):
        self.moved[uid] = folder
        self.unseen.pop(uid, None)

    def close(self):
        pass


def make(tmp_path, fake, box, **kw):
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", imap_host="imap.example.com",
                 imap_user="receipts@example.com", imap_password="pw", mail_allowed_senders=ME, **kw)
    app = create_app(s, extractor=fake, mailbox_factory=lambda: box)
    return TestClient(app), app


def test_allowed_and_verified_mail_is_queued_and_moved_to_processed(tmp_path, fake):
    fake.reply = good_reply(merchant="Uber")
    box = FakeBox({"1": mail()})
    client, app = make(tmp_path, fake, box)
    assert app.state.mail.run_once() == {"queued": 1, "rejected": 0, "unusable": 0}
    assert box.moved == {"1": "Processed"}
    app.state.worker.drain()
    t = client.get("/api/transactions").json()[0]
    assert t["source"] == "email" and t["merchant"] == "Uber" and "Total: Rs 212.40" in fake.calls[-1]["text"]


def test_unknown_sender_and_spoofed_sender_never_reach_the_model(tmp_path, fake):
    box = FakeBox({
        "1": mail(sender="stranger@evil.test"),
        "2": mail(sender=ME, auth=False),                        # forged From: no dmarc=pass
        "3": mail(sender=f"Me <{ME}>, other@x.test"),           # two senders
    })
    client, app = make(tmp_path, fake, box)
    assert app.state.mail.run_once() == {"queued": 0, "rejected": 3, "unusable": 0}
    assert set(box.moved.values()) == {"Rejected"}
    app.state.worker.drain()
    assert client.get("/api/transactions").json() == [] and fake.calls == []


def test_auth_check_can_be_switched_off_for_providers_without_the_header(tmp_path, fake):
    fake.reply = good_reply()
    box = FakeBox({"1": mail(auth=False)})
    client, app = make(tmp_path, fake, box, mail_require_auth=False)
    assert app.state.mail.run_once()["queued"] == 1


def test_display_name_and_case_in_sender_are_handled(tmp_path, fake):
    box = FakeBox({"1": mail(sender="Asish B <ME@Example.com>")})
    _, app = make(tmp_path, fake, box)
    assert app.state.mail.run_once()["queued"] == 1


def test_email_without_usable_content_is_set_aside(tmp_path, fake):
    box = FakeBox({"1": mail(body="   ")})
    _, app = make(tmp_path, fake, box)
    assert app.state.mail.run_once() == {"queued": 0, "rejected": 0, "unusable": 1}
    assert box.moved == {"1": "Rejected"}


def test_oversized_message_is_rejected(tmp_path, fake):
    box = FakeBox({"1": mail(body="x" * 200)})
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", imap_host="h", imap_user="u",
                 imap_password="p", mail_allowed_senders=ME, max_upload_bytes=100)
    app = create_app(s, extractor=fake, mailbox_factory=lambda: box)
    assert app.state.mail.run_once()["rejected"] == 1


def test_mail_failure_is_reported_not_raised(tmp_path, fake):
    def boom():
        raise imaplib.IMAP4.error("LOGIN failed")
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", imap_host="h", imap_user="u",
                 imap_password="p", mail_allowed_senders=ME)
    app = create_app(s, extractor=fake, mailbox_factory=boom)
    assert app.state.mail.run_once() is None and "LOGIN failed" in app.state.mail.last_error


def test_mail_is_off_without_an_allowlist(tmp_path, fake):
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", imap_host="h", imap_user="u", imap_password="p")
    app = create_app(s, extractor=fake)
    assert app.state.mail is None and not s.mail_enabled


def test_config_shows_the_inbox_address(tmp_path, fake):
    client, _ = make(tmp_path, fake, FakeBox({}), mail_address="receipts@abalakrishnan.in")
    assert client.get("/api/config").json()["mail_inbox"] == "receipts@abalakrishnan.in"


# ---- the imaplib wrapper, against a scripted stand-in for the server --------------------------------

class FakeImap:
    log = []

    def __init__(self, host, port, timeout=None):
        self.log.append(("connect", host, port))

    def login(self, u, p):
        self.log.append(("login", u))

    def select(self, folder):
        self.log.append(("select", folder))
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        self.log.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b"7 9"]
        if cmd == "FETCH":
            return "OK", [(b"1 (UID " + args[0] + b" BODY[] {5}", b"RAW-" + args[0]), b")"]
        return "OK", [b""]

    def create(self, folder):
        raise imaplib.IMAP4.error("exists")

    def expunge(self):
        self.log.append(("expunge",))

    def logout(self):
        self.log.append(("logout",))


def test_imap_wrapper_fetches_peeks_and_moves(monkeypatch):
    FakeImap.log = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    box = mailbox.ImapMailbox("imap.example.com", 993, "u", "p", "INBOX")
    assert box.fetch_unseen() == [("7", b"RAW-7"), ("9", b"RAW-9")]
    assert ("FETCH", b"7", "(BODY.PEEK[])") in FakeImap.log        # PEEK: reading must not mark mail as seen
    box.move("7", "Processed")
    assert ("COPY", "7", "Processed") in FakeImap.log and ("STORE", "7", "+FLAGS", r"(\Deleted)") in FakeImap.log
    assert ("expunge",) in FakeImap.log
    box.close()
