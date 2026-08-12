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
]

TRIP_DURATION_DAYS = 7
CURRENCY = "eur"

# A Notion "title" mezőjének a neve. Alapértelmezetten "Name",
# hacsak nem nevezted át az adatbázis létrehozásakor.
TITLE_PROPERTY_NAME = "Name"

# --- Titkos kulcsok beolvasása (GitHub Secrets-ből jönnek) ---

try:
    NOTION_API_KEY = os.environ["NOTION_API_KEY"]
    NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
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
    """Kiszámolja a következő májust YYYY-MM formátumban."""
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
        "min_trip_duration": TRIP_DURATION_DAYS,
        "max_trip_duration": TRIP_DURATION_DAYS,
        "currency": CURRENCY,
        "token": TRAVELPAYOUTS_TOKEN,
    }
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
        print(f"Lekérdezés: {origin} -> {destination}")
        try:
            results = fetch_prices(origin, destination, month)
        except requests.RequestException as e:
            print(f"  Hálózati hiba, kihagyva: {e}")
            skipped_routes.append(f"{origin}-{destination}")
            continue

        print(f"  {len(results)} találat")

        for item in results:
            depart_date = item["departure_at"][:10]
            return_date = item["return_at"][:10]
            price = item["price"]
            link = item.get("link")

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
