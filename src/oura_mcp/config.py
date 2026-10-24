"""Konfiguraatio: client_id/secret .env-tiedostosta, polut ja vakiot."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

API_BASE = "https://api.ouraring.com"
AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SCOPES = "personal daily heartrate workout tag session spo2 heart_health stress ring_configuration"
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
TIMEZONE = "Europe/Helsinki"

DATA_DIR = Path(os.environ.get("OURA_MCP_HOME", Path.home() / ".oura-mcp"))
DB_PATH = DATA_DIR / "oura.duckdb"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    api_base: str = API_BASE

    @classmethod
    def load(cls) -> "Settings":
        # .env luetaan työhakemistosta ja ~/.oura-mcp/.env:stä; ympäristömuuttujat voittavat.
        load_dotenv()
        load_dotenv(DATA_DIR / ".env")
        cid = os.environ.get("OURA_CLIENT_ID", "").strip()
        secret = os.environ.get("OURA_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            raise ConfigError(
                "OURA_CLIENT_ID / OURA_CLIENT_SECRET puuttuvat. Kopioi .env.example -> .env "
                "(tai ~/.oura-mcp/.env) ja täytä arvot."
            )
        return cls(
            client_id=cid,
            client_secret=secret,
            redirect_uri=os.environ.get("OURA_REDIRECT_URI", DEFAULT_REDIRECT_URI),
            api_base=os.environ.get("OURA_API_BASE", API_BASE),
        )
