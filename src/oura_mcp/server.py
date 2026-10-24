"""MCP-palvelin (stdio). Työkalut palauttavat aggregoitua dataa, ei raakasarjoja."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .service import METRICS, Service

mcp = FastMCP("oura")
svc = Service()
_M = ", ".join(METRICS)


@mcp.tool()
def get_daily_summary(start_date: str, end_date: str) -> dict:
    """Yksi rivi per päivä: unipisteet, readiness, aktiivisuuspisteet, unen kesto (h), syvä/REM/kevyt (min), keskisyke,
    HRV, leposyke (yön matalin), askeleet, kalorit, kehon lämpöpoikkeama, stressi. Käytä tätä ensisijaisena
    yleiskatsauksena päiviin/viikkoihin. PAKOLLISET: start_date, end_date (YYYY-MM-DD, aikavyöhyke Europe/Helsinki).
    Max 366 päivää. Tuoreimmat puuttuvat päivät haetaan APIsta; huomautukset (notes) kertovat jos esim. uni puuttuu
    koska sormusta ei ole synkattu."""
    return svc.daily_summary(start_date, end_date)


@mcp.tool()
def get_sleep_detail(date: str) -> dict:
    """Yhden yön täydet tiedot: unipisteet ja contributors-erittely (syvä uni, tehokkuus, viive, REM, levollisuus,
    ajoitus, kokonaisuni), kaikki unijaksot (myös päiväunet) tiivistettynä (univaihe-minuutit, syke/HRV min/max/ka),
    sekä readiness-contributorit. Käytä kun käyttäjä kysyy tietystä yöstä tarkemmin. PAKOLLINEN: date (YYYY-MM-DD,
    se päivä jona heräsit)."""
    return svc.sleep_detail(date)


@mcp.tool()
def get_workouts(start_date: str, end_date: str) -> dict:
    """Treenilista aikavälillä (laji, kesto, kalorit, matka, intensiteetti) sekä yhteenveto lajeittain. Käytä
    treeni- ja harjoitusmääräkysymyksiin. PAKOLLISET: start_date, end_date (YYYY-MM-DD)."""
    return svc.workouts(start_date, end_date)


@mcp.tool(description=(
    f"""Yhden mittarin trendi: keskiarvo/mediaani/min/max/hajonta, trendin suunta ja kulmakerroin (per viikko),
    ensimmäisen ja toisen puoliskon keskiarvo, viikoittainen 7 pv liukuva keskiarvo sekä viikko- (≤4 kk) tai
    kuukausiaggregaatit (pidempi jakso). PAKOLLISET: metric (yksi: {_M}), period ('30d', '12w', '6m', '1y' tai
    'YYYY-MM-DD..YYYY-MM-DD'). Valinnainen end_date; oletus = viimeisin päivä jolla mittarille on dataa."""))
def get_trends(metric: str, period: str, end_date: str | None = None) -> dict:
    return svc.trends(metric, period, end_date)


@mcp.tool(description=(
    f"""Vertaa yhtä mittaria kahden jakson välillä (n, keskiarvo, mediaani, min, max, hajonta, erotus B−A ja
    prosenttimuutos). Käytä kun kysytään 'onko X parantunut/huonontunut' tai kahden ajanjakson vertailua.
    PAKOLLISET: metric (yksi: {_M}), period_a ja period_b muodossa 'YYYY-MM-DD..YYYY-MM-DD' (tai '30d' = viimeiset 30 pv)."""))
def compare_periods(metric: str, period_a: str, period_b: str) -> dict:
    return svc.compare_periods(metric, period_a, period_b)


@mcp.tool()
def get_heartrate_summary(date: str) -> dict:
    """Yhden päivän (kalenteripäivä, Helsingin aika) syke tunneittain: n, min, q1, mediaani, q3, max, ka — sekä
    lähdejakauma (rest/sleep/workout/awake…) osuuksineen. EI palauta raakasarjaa. PAKOLLINEN: date (YYYY-MM-DD)."""
    return svc.heartrate_summary(date)


@mcp.tool()
def query_sql(sql: str) -> dict:
    """Vain-luku SQL (yksi SELECT/WITH-lause) paikalliseen DuckDB-kantaan vapaisiin analyyseihin; max 500 riviä,
    käytä GROUP BY -aggregointia. Taulut: daily_sleep, sleep, daily_activity, daily_readiness, daily_stress,
    daily_spo2, daily_resilience, daily_cardiovascular_age, vo2_max, workout, session, enhanced_tag, heartrate
    (5 min, iso — aggregoi aina) sekä valmis näkymä daily_summary (yksi rivi/päivä). Jokaisella taululla on `raw`
    JSON-sarake (esim. raw->>'$.field'). Aja ensin DESCRIBE-tyyppinen kysely information_schema.columns:sta jos
    sarakkeet epäselviä. PAKOLLINEN: sql."""
    return svc.query_sql(sql)


@mcp.tool()
def export_csv(table: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    """Kirjoittaa taulun (tai daily_summary) CSV-tiedostoksi ~/.oura-mcp/exports/ ja palauttaa polun, rivimäärän ja
    sarakkeet — ei dataa kontekstiin. Käytä kun käyttäjä haluaa datan Exceliin/muuhun työkaluun. PAKOLLINEN: table;
    valinnaiset start_date, end_date (YYYY-MM-DD)."""
    return svc.export_csv(table, start_date, end_date)


def run() -> None:
    mcp.run()
