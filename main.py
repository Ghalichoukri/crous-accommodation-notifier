import json
import logging
import os
import requests
import telepot
from bs4 import BeautifulSoup
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.INFO,
)
logger = logging.getLogger("crous_notifier")

BASE_URL = "https://trouverunlogement.lescrous.fr/tools/42/search"
BOUNDS = "1.4462445_49.241431_3.5592208_48.1201456"
SEEN_FILE = Path("seen_ids.json")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_TELEGRAM_ID = os.environ["MY_TELEGRAM_ID"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen_ids(ids: set):
    SEEN_FILE.write_text(json.dumps(list(ids)))


def fetch_accommodations() -> list:
    accommodations = []
    page = 1
    while True:
        params = {
            "bounds": BOUNDS,
            "page": page,
        }
        r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
        logger.info(f"Page {page} - status {r.status_code}")
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("ul li a[href*='/accommodations/']")
        if not items:
            # Try alternative selector
            items = soup.select("a[href*='/tools/42/accommodations/']")
        
        found_on_page = []
        for item in items:
            href = item.get("href", "")
            if "/accommodations/" not in href:
                continue
            acc_id = href.split("/accommodations/")[-1].strip("/")
            if not acc_id.isdigit():
                continue
            name = item.select_one("h3, h2, .title")
            name_text = name.get_text(strip=True) if name else href
            price = item.select_one(".price, [class*='price']")
            price_text = price.get_text(strip=True) if price else "?"
            found_on_page.append({
                "id": acc_id,
                "name": name_text,
                "price": price_text,
                "url": f"https://trouverunlogement.lescrous.fr/tools/42/accommodations/{acc_id}",
            })

        logger.info(f"Found {len(found_on_page)} on page {page}")
        if not found_on_page:
            break
        accommodations.extend(found_on_page)

        # Check if there's a next page
        next_btn = soup.select_one("a[rel='next'], .next a, [aria-label='Page suivante']")
        if not next_btn:
            break
        page += 1

    return accommodations


def format_message(item: dict) -> str:
    return (
        f"🏠 *Nouveau logement CROUS Île-de-France !*\n"
        f"📍 {item['name']}\n"
        f"💶 {item['price']}\n"
        f"🔗 {item['url']}"
    )


if __name__ == "__main__":
    bot = telepot.Bot(token=TELEGRAM_BOT_TOKEN)
    bot.getMe()
    logger.info("Bot OK")

    seen_ids = set()  # TEMP: enlever cette ligne après le premier test réussi
    # seen_ids = load_seen_ids()
    logger.info(f"Seen IDs: {len(seen_ids)}")

    accommodations = fetch_accommodations()
    logger.info(f"Total found: {len(accommodations)}")

    new_ones = [a for a in accommodations if a["id"] not in seen_ids]
    logger.info(f"New: {len(new_ones)}")

    for item in new_ones:
        msg = format_message(item)
        try:
            bot.sendMessage(MY_TELEGRAM_ID, msg, parse_mode="Markdown")
            logger.info(f"Notified: {item['name']}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")
        seen_ids.add(item["id"])

    save_seen_ids(seen_ids)
    logger.info("Done")
