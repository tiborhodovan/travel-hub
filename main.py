"""
Travel Hub - napi repülőjegy árfigyelő
Minden futáskor lekéri a beállított útvonalakra a legolcsóbb 7 napos
utakat a következő májusra, és frissíti a Notion adatbázist.
"""

import os
import sys
from datetime import date

import requests
from notion_client import Client

# --- Beállítások ---

# (honnan, hova) párok - IATA repülőtér/város kódokkal
ROUTES = [
    ("BUD", "EDI"),  # Budapest -> Edinburgh
    ("BUD", "GLA"),  # Budapest -> Glasgow
    ("VIE", "EDI"),  # Bécs -> Edinburgh
    # ("BUD", "LON"),  # ideiglenes teszt-útvonal - vedd ki a # jelet, ha a fentiek
    #                  # továbbra is 0 találatot adnak, hogy lássuk, egy forgalmas
    #                  # útvonalon egyáltalán jön-e adat a market=en beállítással
]

# A pontosan 7 napos szűrés túl szigorúnak bizonyult ezekre a kisebb
# forgalmú útvonalakra (a cache-alapú API-ban alig van rá találat) - ezért
# egy kis rugalmasságot engedünk: 6-8 nap közötti utakat is elfogadunk,
# és a legolcsóbbat választjuk közülük naponta.
TRIP_MIN_DURATION = 6
TRIP_MAX_DURATION = 8
CURRENCY = "eur"

# Piac-kód induló város szerint - lehet, hogy a helyi piac cache-ében
# több a találat, mint az általános nemzetközi ("en") piacén.
MARKET_BY_ORIGIN = {
    "BUD": "hu",
    "VIE": "at",
}
DEFAULT_MARKET = "en"

# A Notion "title" mezőjének a neve. Alapértelmezetten "Name",
# hacsak nem nevezted át az adatbázis létrehozásakor.
TITLE_PROPERTY_NAME = "Name"

# --- Titkos kulcsok beolvasása (GitHub Secrets-ből jönnek) ---

def normalize_notion_id(raw_id: str) -> str:
    """Kitisztítja a Notion azonosítót, ha véletlenül a teljes URL vagy a
    '?v=...' nézet-azonosító rész is bekerült a másolt szövegbe - csak a
    tiszta ID-t tartja meg."""
    cleaned = raw_id.split("?")[0]          # '?v=...' rész levágása
    cleaned = cleaned.rstrip("/")
    cleaned = cleaned.split("/")[-1]        # ha a teljes URL-t másolták be
    return cleaned                          # a Notion API kötőjellel/anélkül is elfogadja


try:
    NOTION_API_KEY = os.environ["NOTION_API_KEY"]
    NOTION_DATABASE_ID = normalize_notion_id(os.environ["NOTION_DATABASE_ID"])
    TRAVELPAYOUTS_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
except KeyError as e:
    sys.exit(f"Hiányzó környezeti változó: {e}. Állítsd be a GitHub Secrets-ben (vagy lokálisan exportáld).")

notion = Client(auth=NOTION_API_KEY)


def get_data_source_id() -> str:
    """2025 szeptembere óta a Notion külön kezeli az adatbázist (a 'tartót')
    és az azon belüli data source-t (a tényleges táblát, mezőkkel és
    sorokkal). A lekérdezéshez és az új sorok létrehozásához a data
    source ID kell, nem a database ID. Egyszerű, nem megosztott
    adatbázisnál mindig pontosan egy data source tartozik hozzá - ezt
    kérjük le itt egyszer."""
    db = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
    data_sources = db.get("data_sources", [])
    if not data_sources:
        sys.exit("Nem található data source ehhez az adatbázishoz - ellenőrizd a NOTION_DATABASE_ID-t.")
    return data_sources[0]["id"]


def get_target_month() -> str:
    """Kiszámolja a célhónapot. Ha be van állítva a TEST_MONTH környezeti
    változó (pl. a workflow kézi indításánál teszteléshez), azt használja -
    így közelebbi hónapokkal is ki lehet próbálni, van-e egyáltalán adat
    az adott útvonalra. Egyébként a következő májust számolja ki."""
    override = os.environ.get("TEST_MONTH", "").strip()
    if override:
        return override
    today = date.today()
    year = today.year if today.month <= 5 else today.year + 1
    return f"{year}-05"


def fetch_prices(origin: str, destination: str, month: str) -> list[dict]:
    """Lekéri a Travelpayouts API-tól a legolcsóbb, pontosan 7 napos
    utakat a megadott hónap minden napjára."""
    url = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "group_by": "departure_at",
        "currency": CURRENCY,
        "market": MARKET_BY_ORIGIN.get(origin, DEFAULT_MARKET),
        "one_way": "false",  # csak oda-vissza jegyek kellenek, ne egyirányúak
        "token": TRAVELPAYOUTS_TOKEN,
    }

    # Diagnosztikai kapcsoló: ha be van kapcsolva, nem szűrünk az utazás
    # hosszára, hogy lássuk, van-e EGYÁLTALÁN cache-elt adat az útvonalra.
    if os.environ.get("TEST_ANY_DURATION", "").strip().lower() not in ("1", "true", "yes"):
        params["min_trip_duration"] = TRIP_MIN_DURATION
        params["max_trip_duration"] = TRIP_MAX_DURATION
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        print(f"  Hiba a {origin}->{destination} lekérdezésnél: {payload.get('error')}")
        return []

    return list(payload.get("data", {}).values())


def load_existing_pages(data_source_id: str) -> dict:
    """Beolvassa a Notion adatbázis összes meglévő sorát egyetlen menetben,
    hogy tudjuk, melyik (honnan, hova, odautazás dátuma) kombinációhoz
    van már sor - ezt frissítjük majd insert helyett."""
    existing = {}
    cursor = None

    while True:
        query = {"data_source_id": data_source_id, "page_size": 100}
        if cursor:
            query["start_cursor"] = cursor

        result = notion.data_sources.query(**query)

        for page in result["results"]:
            props = page["properties"]
            try:
                origin = props["Honnan"]["rich_text"][0]["text"]["content"]
                destination = props["Hova"]["rich_text"][0]["text"]["content"]
                depart_date = props["Odautazás dátuma"]["date"]["start"]
            except (KeyError, IndexError, TypeError):
                continue
            existing[(origin, destination, depart_date)] = page["id"]

        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return existing


def build_properties(origin, destination, depart_date, return_date, price, link) -> dict:
    title = f"{origin} → {destination} ({depart_date})"
    full_link = f"https://www.aviasales.com{link}" if link else None

    return {
        TITLE_PROPERTY_NAME: {"title": [{"text": {"content": title}}]},
        "Honnan": {"rich_text": [{"text": {"content": origin}}]},
        "Hova": {"rich_text": [{"text": {"content": destination}}]},
        "Odautazás dátuma": {"date": {"start": depart_date}},
        "Hazautazás dátuma": {"date": {"start": return_date}},
        "Ár": {"number": price},
        "Utolsó frissítés": {"date": {"start": date.today().isoformat()}},
        "Foglalási link": {"url": full_link},
    }


def main():
    data_source_id = get_data_source_id()
    month = get_target_month()
    print(f"Célhónap: {month}")

    existing_pages = load_existing_pages(data_source_id)
    print(f"Meglévő sorok a Notionban: {len(existing_pages)}")

    updated = 0
    created = 0
    skipped_routes = []

    for origin, destination in ROUTES:
        market = MARKET_BY_ORIGIN.get(origin, DEFAULT_MARKET)
        print(f"Lekérdezés: {origin} -> {destination} (piac: {market})")
        try:
            results = fetch_prices(origin, destination, month)
        except requests.RequestException as e:
            print(f"  Hálózati hiba, kihagyva: {e}")
            skipped_routes.append(f"{origin}-{destination}")
            continue

        print(f"  {len(results)} találat")

        show_debug = os.environ.get("TEST_ANY_DURATION", "").strip().lower() in ("1", "true", "yes")

        for item in results:
            return_at = item.get("return_at")
            if not return_at:
                continue  # egyirányú jegy, nekünk oda-vissza kell, kihagyjuk

            depart_date = item["departure_at"][:10]
            return_date = return_at[:10]
            price = item["price"]
            link = item.get("link")

            if show_debug:
                nights = (date.fromisoformat(return_date) - date.fromisoformat(depart_date)).days
                print(f"    {depart_date} -> {return_date} ({nights} nap), ár: {price}")

            key = (origin, destination, depart_date)
            props = build_properties(origin, destination, depart_date, return_date, price, link)

            if key in existing_pages:
                notion.pages.update(page_id=existing_pages[key], properties=props)
                updated += 1
            else:
                notion.pages.create(
                    parent={"type": "data_source_id", "data_source_id": data_source_id},
                    properties=props,
                )
                created += 1

    print(f"\nKész. Frissítve: {updated} sor, létrehozva: {created} sor.")
    if skipped_routes:
        print(f"Kihagyott útvonalak (hiba miatt): {', '.join(skipped_routes)}")


if __name__ == "__main__":
    main()
