"""Oura API v2 -asiakas: sivutus, 401-refresh, 429-backoff Retry-After-otsakkeen mukaan."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterator, Literal

import httpx

from . import auth
from .config import Settings

log = logging.getLogger("oura_mcp.api")

RangeKind = Literal["date", "datetime", "none"]


@dataclass(frozen=True)
class Endpoint:
    name: str
    range: RangeKind = "date"
    fields: bool = True  # tukeeko `fields`-parametria
    paginated: bool = True


ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in [
        Endpoint("daily_sleep"),
        Endpoint("sleep"),
        Endpoint("daily_activity"),
        Endpoint("daily_readiness"),
        Endpoint("daily_stress"),
        Endpoint("daily_spo2"),
        Endpoint("daily_resilience", fields=False),
        Endpoint("daily_cardiovascular_age"),
        Endpoint("vO2_max"),
        Endpoint("workout"),
        Endpoint("session"),
        Endpoint("enhanced_tag", fields=False),
        Endpoint("tag", fields=False),
        Endpoint("heartrate", range="datetime"),
        Endpoint("ring_battery_level", range="datetime"),
        Endpoint("ring_configuration", range="none"),
        Endpoint("personal_info", range="none", paginated=False),
    ]
}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ScopeOrSubscriptionError(ApiError):
    """403: tilaus (Oura Membership) vanhentunut tai scope puuttuu."""


class RateLimited(ApiError):
    def __init__(self, message: str, retry_after: float | None, info: dict[str, str]):
        super().__init__(429, message)
        self.retry_after = retry_after
        self.info = info


RATE_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Window", "X-RateLimit-Reset", "X-RateLimit-Tier")


def _rate_info(r: httpx.Response) -> dict[str, str]:
    return {h: r.headers[h] for h in RATE_HEADERS if h in r.headers}


class OuraClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sandbox: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        max_wait: float = 120.0,
    ):
        self.settings = settings
        self.sandbox = sandbox
        self.sleep = sleep
        self.max_retries = max_retries
        self.max_wait = max_wait  # yli tämän Retry-After -> nostetaan virhe eikä jäädä jumiin
        base = settings.api_base if settings else "https://api.ouraring.com"
        self.http = client or httpx.Client(base_url=base, timeout=30)
        self.prefix = "/v2/sandbox/usercollection" if sandbox else "/v2/usercollection"

    # ------------------------------------------------------------------ low level
    def _headers(self) -> dict[str, str]:
        if self.sandbox and self.settings is None:
            return {}
        try:
            return {"Authorization": f"Bearer {auth.get_access_token(self.settings)}"}
        except auth.NotAuthenticated:
            if self.sandbox:  # sandbox ei vaadi tokenia
                return {}
            raise

    def _request(self, path: str, params: dict[str, Any]) -> dict:
        refreshed = False
        attempt = 0
        while True:
            r = self.http.get(path, params=params, headers=self._headers())
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and not self.sandbox:
                if refreshed:
                    raise auth.AuthError(f"401 myös tokenin uusimisen jälkeen. {auth.REAUTH_HINT}")
                refreshed = True
                auth.refresh_tokens(self.settings)  # nostaa AuthErrorin ohjeella jos epäonnistuu
                continue
            if r.status_code == 403:
                raise ScopeOrSubscriptionError(
                    403,
                    "403: Oura-tilaus (membership) on vanhentunut tai tokenilta puuttuu tarvittava scope. "
                    f"Tarkista tilaus ja aja tarvittaessa `oura-mcp auth`. Vastaus: {r.text[:200]}",
                )
            if r.status_code == 429:
                info = _rate_info(r)
                ra = _parse_retry_after(r.headers.get("Retry-After"))
                attempt += 1
                if attempt > self.max_retries or (ra is not None and ra > self.max_wait):
                    raise RateLimited(
                        f"429 rate limit (Retry-After={ra}s, {info}). Yritä myöhemmin uudelleen.", ra, info
                    )
                wait = ra if ra is not None else min(2.0**attempt, 60.0)  # kiinteä viive vain jos otsake puuttuu
                log.warning("429, odotetaan %.1fs (%s)", wait, info)
                self.sleep(wait)
                continue
            raise ApiError(r.status_code, f"Oura API {r.status_code}: {r.text[:300]}")

    # ------------------------------------------------------------------ high level
    def iter_pages(
        self,
        endpoint: str,
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
        fields: list[str] | None = None,
    ) -> Iterator[list[dict]]:
        spec = ENDPOINTS[endpoint]
        params: dict[str, Any] = {}
        if spec.range == "date":
            if start is not None:
                params["start_date"] = _iso(start)
            if end is not None:
                params["end_date"] = _iso(end)
        elif spec.range == "datetime":
            # httpx koodaa '+' -> %2B parametriarvoissa automaattisesti
            if start is not None:
                params["start_datetime"] = _iso(start)
            if end is not None:
                params["end_datetime"] = _iso(end)
        if fields:
            if not spec.fields:
                raise ValueError(f"{endpoint} ei tue fields-parametria")
            params["fields"] = fields

        path = f"{self.prefix}/{endpoint}"
        seen: set[str] = set()
        while True:
            body = self._request(path, params)
            if not spec.paginated:
                yield [body]
                return
            yield body.get("data", [])
            token = body.get("next_token")
            if not token:
                return
            if token in seen:
                raise ApiError(0, "next_token toistuu — keskeytetään sivutus")
            seen.add(token)
            params["next_token"] = token

    def get_all(self, endpoint: str, start=None, end=None, fields: list[str] | None = None) -> list[dict]:
        return [row for page in self.iter_pages(endpoint, start, end, fields) for row in page]

    def close(self) -> None:
        self.http.close()


def _iso(v: date | datetime | str) -> str:
    return v if isinstance(v, str) else v.isoformat()


def _parse_retry_after(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None
