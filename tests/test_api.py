import os

import httpx
import pytest
import respx

from oura_mcp import auth
from oura_mcp.api import ApiError, OuraClient, RateLimited, ScopeOrSubscriptionError
from oura_mcp.config import TOKEN_URL, Settings

S = Settings(client_id="c", client_secret="s")
BASE = "https://api.ouraring.com/v2/sandbox/usercollection"


@pytest.fixture
def sandbox_client():
    sleeps = []
    c = OuraClient(S, sandbox=True, sleep=sleeps.append)
    c.sleeps = sleeps
    return c


@pytest.fixture
def live_client():
    import time
    auth.save_tokens({"access_token": "T1", "refresh_token": "R1", "expires_at": time.time() + 3600, "scope": "x"})
    sleeps = []
    c = OuraClient(S, sleep=sleeps.append)
    c.sleeps = sleeps
    return c


@respx.mock
def test_pagination_follows_next_token_until_null(sandbox_client):
    route = respx.get(f"{BASE}/daily_sleep")
    route.side_effect = [
        httpx.Response(200, json={"data": [{"day": "2024-01-01"}], "next_token": "A"}),
        httpx.Response(200, json={"data": [{"day": "2024-01-02"}], "next_token": "B"}),
        httpx.Response(200, json={"data": [{"day": "2024-01-03"}], "next_token": None}),
    ]
    rows = sandbox_client.get_all("daily_sleep", "2024-01-01", "2024-01-03")
    assert [r["day"] for r in rows] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert route.call_count == 3
    q = route.calls[1].request.url.params
    assert q["next_token"] == "A" and q["start_date"] == "2024-01-01"
    assert route.calls[2].request.url.params["next_token"] == "B"


@respx.mock
def test_repeating_next_token_aborts(sandbox_client):
    respx.get(f"{BASE}/sleep").mock(return_value=httpx.Response(200, json={"data": [], "next_token": "LOOP"}))
    with pytest.raises(ApiError, match="toistuu"):
        sandbox_client.get_all("sleep", "2024-01-01", "2024-01-02")


@respx.mock
def test_heartrate_plus_in_timezone_encoded(sandbox_client):
    route = respx.get(f"{BASE}/heartrate").mock(return_value=httpx.Response(200, json={"data": [], "next_token": None}))
    sandbox_client.get_all("heartrate", "2024-01-01T00:00:00+02:00", "2024-01-02T00:00:00+02:00")
    raw = str(route.calls[0].request.url)
    assert "%2B02%3A00" in raw or "%2B02:00" in raw
    assert "+02" not in raw


@respx.mock
def test_fields_rejected_for_unsupported_endpoint(sandbox_client):
    with pytest.raises(ValueError):
        sandbox_client.get_all("daily_resilience", "2024-01-01", "2024-01-02", fields=["day"])


@respx.mock
def test_429_uses_retry_after_not_fixed_delay(sandbox_client):
    route = respx.get(f"{BASE}/workout")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "7", "X-RateLimit-Tier": "standard"}),
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json={"data": [{"id": 1}], "next_token": None}),
    ]
    rows = sandbox_client.get_all("workout", "2024-01-01", "2024-01-02")
    assert rows == [{"id": 1}]
    assert sandbox_client.sleeps == [7.0, 3.0]


@respx.mock
def test_429_without_header_falls_back_to_exponential(sandbox_client):
    route = respx.get(f"{BASE}/workout")
    route.side_effect = [httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"data": [], "next_token": None})]
    sandbox_client.get_all("workout", "2024-01-01", "2024-01-02")
    assert sandbox_client.sleeps == [2.0, 4.0]


@respx.mock
def test_429_gives_up_after_max_retries(sandbox_client):
    respx.get(f"{BASE}/workout").mock(return_value=httpx.Response(429, headers={"Retry-After": "1", "X-RateLimit-Limit": "5000"}))
    with pytest.raises(RateLimited) as e:
        sandbox_client.get_all("workout", "2024-01-01", "2024-01-02")
    assert e.value.info == {"X-RateLimit-Limit": "5000"}
    assert len(sandbox_client.sleeps) == sandbox_client.max_retries


@respx.mock
def test_429_with_huge_retry_after_raises_instead_of_blocking(sandbox_client):
    respx.get(f"{BASE}/workout").mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(RateLimited):
        sandbox_client.get_all("workout", "2024-01-01", "2024-01-02")
    assert sandbox_client.sleeps == []


@respx.mock
def test_403_clear_error(sandbox_client):
    respx.get(f"{BASE}/daily_sleep").mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(ScopeOrSubscriptionError, match="scope"):
        sandbox_client.get_all("daily_sleep", "2024-01-01", "2024-01-02")


@respx.mock
def test_401_refreshes_once_and_retries(live_client):
    url = "https://api.ouraring.com/v2/usercollection/daily_sleep"
    route = respx.get(url)
    route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"data": [{"day": "d"}], "next_token": None}),
    ]
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "T2", "refresh_token": "R2", "expires_in": 3600}))
    assert live_client.get_all("daily_sleep", "2024-01-01", "2024-01-02") == [{"day": "d"}]
    assert route.calls[0].request.headers["Authorization"] == "Bearer T1"
    assert route.calls[1].request.headers["Authorization"] == "Bearer T2"


@respx.mock
def test_401_after_refresh_gives_reauth_hint(live_client):
    respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "T2", "refresh_token": "R2", "expires_in": 3600}))
    with pytest.raises(auth.AuthError, match="oura-mcp auth"):
        live_client.get_all("daily_sleep", "2024-01-01", "2024-01-02")


@respx.mock
def test_401_and_refresh_failure_gives_reauth_hint(live_client):
    respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(auth.AuthError, match="oura-mcp auth"):
        live_client.get_all("daily_sleep", "2024-01-01", "2024-01-02")


@respx.mock
def test_personal_info_single_object(sandbox_client):
    respx.get(f"{BASE}/personal_info").mock(return_value=httpx.Response(200, json={"id": "x", "age": 40}))
    assert sandbox_client.get_all("personal_info") == [{"id": "x", "age": 40}]


@pytest.mark.skipif(not os.environ.get("OURA_LIVE_SANDBOX"), reason="live sandbox -testi: aseta OURA_LIVE_SANDBOX=1")
def test_live_sandbox_daily_sleep():
    c = OuraClient(None, sandbox=True)
    rows = c.get_all("daily_sleep", "2024-01-01", "2024-01-31")
    assert rows and "day" in rows[0]
