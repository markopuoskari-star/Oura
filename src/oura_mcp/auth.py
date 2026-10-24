"""OAuth2-autentikointi: authorization code -flow, avainnippu, automaattinen refresh."""
from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring
from keyring.errors import KeyringError

from .config import AUTHORIZE_URL, SCOPES, TOKEN_URL, Settings

KEYRING_SERVICE = "oura-mcp"
KEYRING_USER = "oauth-tokens"
EXPIRY_MARGIN_S = 60

REAUTH_HINT = "Aja `oura-mcp auth` uudelleen."


class AuthError(RuntimeError):
    pass


class NotAuthenticated(AuthError):
    def __init__(self, detail: str = "Ei tallennettuja tokeneita."):
        super().__init__(f"{detail} {REAUTH_HINT}")


# ---------------------------------------------------------------- token store

def load_tokens() -> dict | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except KeyringError as e:
        raise AuthError(f"Avainnippu ei käytettävissä: {e}") from e
    return json.loads(raw) if raw else None


def save_tokens(tokens: dict) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(tokens))
    except KeyringError as e:
        raise AuthError(f"Tokenien tallennus avainnippuun epäonnistui: {e}") from e


def clear_tokens() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except KeyringError:
        pass


def _normalize(resp: dict, previous: dict | None = None) -> dict:
    """Token-vastaus -> tallennettava muoto. Oura kierrättää refresh-tokenin (kertakäyttöinen),
    joten uusi refresh_token aina korvaa vanhan; jos sitä ei tule, säilytetään vanha."""
    refresh = resp.get("refresh_token") or (previous or {}).get("refresh_token")
    return {
        "access_token": resp["access_token"],
        "refresh_token": refresh,
        "expires_at": time.time() + int(resp.get("expires_in", 86400)),
        "scope": resp.get("scope", (previous or {}).get("scope", SCOPES)),
    }


# ------------------------------------------------------------------ OAuth flow

def build_authorize_url(settings: Settings, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )


def exchange_code(settings: Settings, code: str, client: httpx.Client | None = None) -> dict:
    return _token_request(
        settings,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.redirect_uri},
        client,
    )


def _token_request(settings: Settings, data: dict, client: httpx.Client | None) -> dict:
    payload = {**data, "client_id": settings.client_id, "client_secret": settings.client_secret}
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(TOKEN_URL, data=payload)
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise AuthError(f"Token-pyyntö epäonnistui ({r.status_code}): {r.text[:300]}")
    return r.json()


def _make_handler(result: dict, expected_state: str, path: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(url.query)
            if q.get("state", [None])[0] != expected_state:
                result["error"] = "state ei täsmää (mahdollinen CSRF)"
            elif "error" in q:
                result["error"] = q["error"][0]
            elif "code" in q:
                result["code"] = q["code"][0]
            else:
                result["error"] = "callbackista puuttuu code"
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Valmis! Voit sulkea tämän ikkunan." if ok else f"Virhe: {result['error']}"
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *args):  # hiljennä
            pass

    return Handler


def run_auth(
    settings: Settings,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = 300,
    client: httpx.Client | None = None,
) -> dict:
    """Koko flow: avaa selain, kuuntele callback, vaihda code tokeneiksi, tallenna avainnippuun."""
    redir = urlparse(settings.redirect_uri)
    host, port = redir.hostname or "localhost", redir.port or 80
    state = secrets.token_urlsafe(24)
    result: dict = {}
    server = HTTPServer((host, port), _make_handler(result, state, redir.path or "/"))
    server.timeout = 0.5

    url = build_authorize_url(settings, state)
    print(f"Avataan selain. Jos ei aukea, mene osoitteeseen:\n{url}\n")
    open_browser(url)

    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise AuthError("Aikakatkaisu: callbackia ei saatu.")
    if "error" in result:
        raise AuthError(f"Valtuutus epäonnistui: {result['error']}")

    tokens = _normalize(exchange_code(settings, result["code"], client))
    save_tokens(tokens)
    return tokens


# --------------------------------------------------------------------- refresh

_refresh_lock = threading.Lock()


def refresh_tokens(settings: Settings, client: httpx.Client | None = None) -> dict:
    """Kertakäyttöinen refresh-token: uusi pari tallennetaan heti."""
    with _refresh_lock:
        current = load_tokens()
        if not current or not current.get("refresh_token"):
            raise NotAuthenticated()
        try:
            resp = _token_request(
                settings,
                {"grant_type": "refresh_token", "refresh_token": current["refresh_token"]},
                client,
            )
        except AuthError as e:
            raise AuthError(f"Tokenin uusiminen epäonnistui ({e}). {REAUTH_HINT}") from e
        tokens = _normalize(resp, current)
        save_tokens(tokens)
        return tokens


def get_access_token(settings: Settings, client: httpx.Client | None = None) -> str:
    tokens = load_tokens()
    if not tokens:
        raise NotAuthenticated()
    if tokens["expires_at"] - EXPIRY_MARGIN_S <= time.time():
        tokens = refresh_tokens(settings, client)
    return tokens["access_token"]
