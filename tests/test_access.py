"""Cloudflare Access tokens, checked against a locally generated key pair (not against real Cloudflare)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEAM = "asish.cloudflareaccess.com"
AUD = "abc123aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
HDR = {"x-requested-with": "receipt-tracker"}


class FixedKeys:
    """Stands in for Cloudflare's key endpoint."""

    class _K:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def token(key=KEY, aud=AUD, iss=f"https://{TEAM}", exp=3600, **extra):
    now = int(time.time())
    return jwt.encode({"aud": [aud], "iss": iss, "iat": now, "exp": now + exp,
                       "email": "me@example.com", **extra}, key, algorithm="RS256")


def client(tmp_path, fake, **kw):
    s = Settings(data_dir=tmp_path, llm_api_key="", token=kw.pop("token", ""), password_hash="", sync_ingest=True,
                 access_team_domain=TEAM, access_aud=AUD, **kw)
    return TestClient(create_app(s, extractor=fake, access_key_client=FixedKeys()))


def h(t, **more):
    return {"cf-access-jwt-assertion": t, **more}


def test_valid_access_token_is_accepted_and_missing_one_is_refused(tmp_path, fake):
    c = client(tmp_path, fake)
    assert c.get("/api/session").json() == {"mode": "access", "authenticated": False}
    assert c.get("/api/transactions").status_code == 401
    assert c.get("/api/transactions", headers=h(token())).status_code == 200
    assert c.get("/api/session", headers=h(token())).json()["authenticated"] is True


@pytest.mark.parametrize("bad", [
    lambda: token(key=OTHER),                                # signed by someone else
    lambda: token(aud="another-app"),                        # meant for a different Access application
    lambda: token(iss="https://evil.cloudflareaccess.com"),  # wrong team
    lambda: token(exp=-10),                                  # expired
    lambda: "not.a.jwt",
])
def test_bad_tokens_are_refused(tmp_path, fake, bad):
    assert client(tmp_path, fake).get("/api/transactions", headers=h(bad())).status_code == 401


def test_unsigned_alg_none_token_is_refused(tmp_path, fake):
    now = int(time.time())
    forged = jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 99, "email": "me@example.com"},
                        None, algorithm="none")
    assert client(tmp_path, fake).get("/api/transactions", headers=h(forged)).status_code == 401


def test_people_need_the_csrf_header_for_writes_but_service_tokens_do_not(tmp_path, fake):
    c = client(tmp_path, fake)
    body = {"merchant": "Chai", "amount": 40, "date": "2026-09-22"}
    assert c.post("/api/transactions", json=body, headers=h(token())).status_code == 403
    assert c.post("/api/transactions", json=body, headers=h(token(), **HDR)).status_code == 201
    svc = token(email=None, common_name="shortcut-client-id")
    payload = jwt.decode(svc, options={"verify_signature": False})
    payload.pop("email")
    svc = jwt.encode(payload, KEY, algorithm="RS256")
    assert c.post("/api/transactions", json=body, headers=h(svc)).status_code == 201


def test_optional_email_allowlist(tmp_path, fake):
    c = client(tmp_path, fake, access_emails="me@example.com")
    assert c.get("/api/transactions", headers=h(token())).status_code == 200
    assert c.get("/api/transactions", headers=h(token(email="someone@else.com"))).status_code == 401


def test_api_token_still_works_beside_access(tmp_path, fake):
    c = client(tmp_path, fake, token="s3cret")
    assert c.get("/api/transactions", headers={"authorization": "Bearer s3cret"}).status_code == 200


def test_require_auth_accepts_access_as_a_login(tmp_path, fake):
    s = Settings(data_dir=tmp_path, llm_api_key="", token="", password_hash="", require_auth=True,
                 access_team_domain=TEAM, access_aud=AUD)
    create_app(s, extractor=fake, access_key_client=FixedKeys())
