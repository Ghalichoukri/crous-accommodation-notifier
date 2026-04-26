import json
import logging
import os
import requests
import telepot
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.INFO,
)
logger = logging.getLogger("crous_notifier")

SEARCH_URL = "https://trouverunlogement.lescrous.fr/api/v1/accommodations/search"
# Île-de-France bounds
BOUNDS = os.environ.get(
    "SEARCH_BOUNDS",
    "1.446289_49.242968_3.559570_48.117274"
)
SEEN_FILE = Path("seen_ids.json")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_TELEGRAM_ID = os.environ["MY_TELEGRAM_ID"]


def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen_ids(ids: set):
    SEEN_FILE.write_text(json.dumps(list(ids)))


def fetch_accommodations() -> list:
    bounds = BOUNDS.split("_")
    params = {
        "bounds[sw][lat]": bounds[3],
        "bounds[sw][lng]": bounds[0],
        "bounds[ne][lat]": bounds[1],
        "bounds[ne][lng]": bounds[2],
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    all_items = []
    page = 1
    while True:
        params["page"] = page
        r = requests.get(
            "https://trouverunlogement.lescrous.fr/api/v1/tools/38/accommodations",
            params=params,
            headers=headers,
            timeout=30,
        )
        logger.info(f"Page {page} - status {r.status_code}")
        if r.status_code != 200:
            logger.error(f"API error: {r.text[:500]}")
            break
        data = r.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            break
        all_items.extend(items)
        total_pages = data.get("data", {}).get("nbPages", 1)
        if page >= total_pages:
            break
        page += 1
    return all_items


def format_message(item: dict) -> str:
    name = item.get("name", "Logement inconnu")
    city = item.get("city", "")
    price = item.get("price", "?")
    area = item.get("area", "?")
    item_id = item.get("id", "")
    url = f"https://trouverunlogement.lescrous.fr/tools/38/accommodations/{item_id}"
    return (
        f"🏠 *Nouveau logement CROUS !*\n"
        f"📍 {name} - {city}\n"
        f"💶 {price}€/mois | {area}m²\n"
        f"🔗 {url}"
    )


if __name__ == "__main__":
    bot = telepot.Bot(token=TELEGRAM_BOT_TOKEN)
    bot.getMe()
    logger.info("Bot OK")

    seen_ids = load_seen_ids()
    logger.info(f"Seen IDs loaded: {len(seen_ids)}")

    accommodations = fetch_accommodations()
    logger.info(f"Found {len(accommodations)} accommodations")

    new_ones = [a for a in accommodations if str(a.get("id")) not in seen_ids]
    logger.info(f"New accommodations: {len(new_ones)}")

    for item in new_ones:
        msg = format_message(item)
        bot.sendMessage(MY_TELEGRAM_ID, msg, parse_mode="Markdown")
        seen_ids.add(str(item.get("id")))
        logger.info(f"Notified: {item.get('name')}")

    save_seen_ids(seen_ids)
    logger.info("Done")
