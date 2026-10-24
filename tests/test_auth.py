import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from oura_mcp import auth
from oura_mcp.config import TOKEN_URL, Settings

S = Settings(client_id="cid", client_secret="sec", redirect_uri="http://localhost:18080/callback")


def token_json(access="A", refresh="R", expires_in=86400):
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in, "scope": "personal daily"}


def test_authorize_url_has_all_scopes():
    q = parse_qs(urlparse(auth.build_authorize_url(S, "st")).query)
    assert q["client_id"] == ["cid"] and q["state"] == ["st"]
    assert set(q["scope"][0].split()) == {"personal", "daily", "heartrate", "workout", "tag", "session", "spo2", "heart_health", "stress", "ring_configuration"}


def test_full_flow_saves_tokens_to_keyring(memory_keyring):
    def fake_browser(url):
        q = parse_qs(urlparse(url).query)
        def hit():
            time.sleep(0.3)
            with httpx.Client() as c:  # paikallinen callback ei mene respx:n läpi, koska mock on vain TOKEN_URL:lle
                c.get(f"http://localhost:18080/callback?code=XYZ&state={q['state'][0]}")
        threading.Thread(target=hit).start()

    with respx.mock(assert_all_called=False, assert_all_mocked=False) as m:
        m.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_json()))
        m.route(host="localhost").pass_through()
        tokens = auth.run_auth(S, open_browser=fake_browser, timeout=10)
    assert tokens["access_token"] == "A"
    assert auth.load_tokens()["refresh_token"] == "R"


def test_state_mismatch_rejected():
    def fake_browser(url):
        def hit():
            time.sleep(0.3)
            httpx.get("http://localhost:18080/callback?code=X&state=WRONG")
        threading.Thread(target=hit).start()

    with pytest.raises(auth.AuthError, match="state"):
        auth.run_auth(S, open_browser=fake_browser, timeout=10)


@respx.mock
def test_refresh_rotates_refresh_token():
    auth.save_tokens({"access_token": "old", "refresh_token": "R1", "expires_at": 0, "scope": "x"})
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_json("new", "R2")))
    assert auth.get_access_token(S) == "new"
    assert auth.load_tokens()["refresh_token"] == "R2"
    assert b"refresh_token=R1" in route.calls[0].request.content


@respx.mock
def test_refresh_failure_gives_reauth_hint():
    auth.save_tokens({"access_token": "old", "refresh_token": "R1", "expires_at": 0, "scope": "x"})
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(auth.AuthError, match="oura-mcp auth"):
        auth.get_access_token(S)


def test_no_tokens_gives_hint():
    with pytest.raises(auth.NotAuthenticated, match="oura-mcp auth"):
        auth.get_access_token(S)


def test_valid_token_not_refreshed():
    auth.save_tokens({"access_token": "ok", "refresh_token": "R", "expires_at": time.time() + 3600, "scope": "x"})
    assert auth.get_access_token(S) == "ok"
