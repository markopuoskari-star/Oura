from __future__ import annotations

import argparse
import sys

from datetime import date

from .auth import AuthError, run_auth
from .api import ApiError, OuraClient
from .config import ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="oura-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth2-kirjautuminen (avaa selaimen)")
    sp = sub.add_parser("sync", help="Hae historia APIsta DuckDB-kantaan (idempotentti)")
    sp.add_argument("--from", dest="start", type=date.fromisoformat, help="alkupäivä YYYY-MM-DD; puuttuessa jatketaan viimeisestä synkatusta")
    sp.add_argument("--to", dest="end", type=date.fromisoformat, help="loppupäivä (oletus: tänään)")
    sp.add_argument("--endpoints", nargs="*", help="rajaa endpointteihin, esim. daily_sleep sleep")
    sp.add_argument("--sandbox", action="store_true", help="käytä /v2/sandbox/-endpointteja")
    sub.add_parser("serve", help="Käynnistä MCP-palvelin (stdio); Claude Desktop kutsuu tätä")
    args = p.parse_args(argv)
    try:
        if args.cmd == "auth":
            tokens = run_auth(Settings.load())
            print(f"Autentikointi onnistui. Scopet: {tokens['scope']}. Tokenit tallennettu avainnippuun.")
        elif args.cmd == "serve":
            from .server import run

            run()
        elif args.cmd == "sync":
            from .db import connect
            from .sync import run_sync

            client = OuraClient(Settings.load(), sandbox=args.sandbox)
            con = connect()
            totals = run_sync(client, con, args.start, args.end, args.endpoints, progress=print)
            print("Valmis:", ", ".join(f"{k}={v}" for k, v in totals.items()))
    except (ConfigError, AuthError, ApiError, ValueError) as e:
        print(f"Virhe: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
