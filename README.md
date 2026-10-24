# oura-mcp

Paikallinen MCP-palvelin, joka tuo Oura Ring -datasi Claude Desktopiin. Kaikki pyörii omalla koneellasi:
OAuth2-tokenit OS:n avainnippuun, data DuckDB-tiedostoon (`~/.oura-mcp/oura.duckdb`), ei pilvipalveluita.

## 1. Asennus

Vaatii Python 3.11+ ja [uv](https://docs.astral.sh/uv/).

```bash
git clone <tämä repo> && cd Oura
uv sync
```

## 2. OAuth-sovelluksen rekisteröinti

Personal access tokenit on poistettu käytöstä (12/2025), joten tarvitset OAuth2-sovelluksen.

1. Mene osoitteeseen <https://cloud.ouraring.com/oauth/applications> ja luo uusi sovellus.
2. **Redirect URI:** `http://localhost:8080/callback` (täsmälleen tämä).
3. **Scopet:** `personal daily heartrate workout tag session spo2 heart_health`.
4. Kopioi client ID ja client secret.

```bash
cp .env.example .env     # täytä OURA_CLIENT_ID ja OURA_CLIENT_SECRET
```

`.env` ja kaikki tokenit ovat `.gitignore`ssa. Vaihtoehtoisesti `.env` voi olla `~/.oura-mcp/.env`.

## 3. Ensimmäinen kirjautuminen

```bash
uv run oura-mcp auth
```

Selain aukeaa, hyväksyt pääsyn, ja tokenit tallentuvat avainnippuun (ei tiedostoon). Access token uusitaan
automaattisesti; Ouran refresh-token on kertakäyttöinen ja uusi pari tallennetaan joka uusinnalla. Jos uusinta
epäonnistuu, saat ohjeen ajaa `oura-mcp auth` uudelleen.

## 4. Ensimmäinen sync

```bash
uv run oura-mcp sync --from 2023-01-01
```

Hakee historian 1–3 kk paloissa (syke 7 pv paloissa) ja upsertaa kantaan, joten komennon voi ajaa uudelleen
turvallisesti. Ilman `--from`-lippua jatketaan viimeisestä synkatusta päivästä (3 pv päällekkäisyydellä, koska
uni/readiness päivittyvät jälkikäteen). Rajaus: `--endpoints daily_sleep sleep`. Ensimmäinen sync voi kestää
kauan syketietojen takia; rate limitin osuessa odotetaan `Retry-After`-otsakkeen verran.

> Uni- ja readiness-data ilmestyy vasta kun avaat Oura-sovelluksen ja sormus synkkaa. Aktiivisuus ja stressi
> päivittyvät taustalla. Aktiivisuuden "päivä" alkaa klo 04:00 (API:n `day`-kenttä hoitaa tämän). Aikavyöhyke
> on Europe/Helsinki.

## 5. Claude Desktop

Lisää `claude_desktop_config.json`iin (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`).
Vaihda polku omaksesi:

```json
{
  "mcpServers": {
    "oura": {
      "command": "uv",
      "args": ["run", "--project", "/POLKU/Oura", "oura-mcp", "serve"]
    }
  }
}
```

`.env` luetaan työhakemistosta ja `~/.oura-mcp/.env`:stä — Claude Desktopin käynnistämän prosessin työhakemisto
ei ole repo, joten **kopioi `.env` kohtaan `~/.oura-mcp/.env`** (tai lisää `"env": {"OURA_CLIENT_ID": "...",
"OURA_CLIENT_SECRET": "..."}` yllä olevaan konfiguraatioon). Käynnistä Claude Desktop uudelleen.

## Työkalut

| Työkalu | Pakolliset parametrit | Mitä |
|---|---|---|
| `get_daily_summary` | start_date, end_date | rivi/päivä: pisteet, uni, HRV, leposyke, askeleet, lämpöpoikkeama |
| `get_sleep_detail` | date | yhden yön contributors + unijaksot tiivistettynä |
| `get_workouts` | start_date, end_date | treenit + lajiyhteenveto |
| `get_trends` | metric, period | keskiarvot, trendin suunta, viikko-/kuukausiaggregaatit |
| `compare_periods` | metric, period_a, period_b | kahden jakson vertailu |
| `get_heartrate_summary` | date | tuntikohtaiset kvartiilit + lähdejakauma |
| `query_sql` | sql | vain-luku SELECT (näkymä `daily_summary`, max 500 riviä) |
| `export_csv` | table | CSV-tiedosto `~/.oura-mcp/exports/` |

Työkalut lukevat ensisijaisesti kannasta. API:sta haetaan vain viimeisen 14 päivän puuttuvat päivät, ja saman
puuttuvan välin hakua ei toisteta 30 minuutin sisällä (rate limit). Vanhemmat aukot täytetään `oura-mcp sync`illä.

## Testit

```bash
uv run pytest
OURA_LIVE_SANDBOX=1 uv run pytest -k live_sandbox   # valinnainen, vaatii verkkoyhteyden api.ouraring.com:iin
```

Testit mockaavat HTTP:n (respx): sivutus, 429-backoff (`Retry-After`), 401-refresh, 403, OAuth-flow ja
DuckDB-/työkalulogiikka synteettisellä datalla.
