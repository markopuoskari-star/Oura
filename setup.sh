#!/usr/bin/env bash
# Asentaa riippuvuudet, luo ~/.oura-mcp/.env:n ja tulostaa Claude Desktop -konfiguraation.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

command -v uv >/dev/null || { echo "uv puuttuu. Asenna: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }

uv sync

mkdir -p "$HOME/.oura-mcp"
ENV_FILE="$HOME/.oura-mcp/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Loin $ENV_FILE"
fi

if grep -qE '^OURA_CLIENT_ID=.+' "$ENV_FILE" && grep -qE '^OURA_CLIENT_SECRET=.+' "$ENV_FILE"; then
  echo "Tunnukset löytyivät."
else
  echo
  echo ">> Täytä OURA_CLIENT_ID ja OURA_CLIENT_SECRET tiedostoon: $ENV_FILE"
  echo "   (OAuth-sovellus: https://cloud.ouraring.com/oauth/applications"
  echo "    Redirect URI: http://localhost:8080/callback"
  echo "    Scopet: personal daily heartrate workout tag session spo2 heart_health)"
fi

cat <<JSON

>> Lisää tämä claude_desktop_config.json-tiedostoon (mcpServers-kohtaan):

    "oura": {
      "command": "$(command -v uv)",
      "args": ["run", "--project", "$REPO", "oura-mcp", "serve"]
    }

>> Seuraavaksi:
   1. uv run oura-mcp auth
   2. uv run oura-mcp sync --from 2026-09-01     (testi; koko historia: --from 2023-01-01)
   3. Käynnistä Claude Desktop uudelleen
JSON
