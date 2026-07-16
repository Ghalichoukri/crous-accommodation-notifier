import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =========================
# CONFIGURATION
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "5"))

SEND_STATUS_EVERY_CHECK = os.getenv("SEND_STATUS_EVERY_CHECK", "false").lower() == "true"
NOTIFY_ALL_AVAILABLE = os.getenv("NOTIFY_ALL_AVAILABLE", "true").lower() == "true"

HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "60"))
SEND_START_MESSAGE = os.getenv("SEND_START_MESSAGE", "false").lower() == "true"

CROUS_URLS_RAW = os.getenv("CROUS_URLS", "").strip()

SEEN_FILE = "seen_accommodations.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# =========================
# URLS PAR DEFAUT
# =========================

DEFAULT_CROUS_TARGETS = [
    {
        "label": "Angers",
        "url": "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=-0.6176931_47.5262993_-0.508546_47.4373546&locationName=Angers",
    },
    {
        "label": "Tours",
        "url": "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=0.6528317_47.4395937_0.7373427_47.3489171&locationName=Tours",
    },
    {
        "label": "Île-de-France",
        "url": "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=1.4462445_49.241431_3.5592208_48.1201456",
    },
]


# =========================
# OUTILS
# =========================

def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_crous_urls(raw_value):
    if not raw_value:
        logging.warning("CROUS_URLS vide. Utilisation des URLs par défaut.")
        return DEFAULT_CROUS_TARGETS

    targets = []

    for part in raw_value.split(","):
        part = part.strip()

        if not part:
            continue

        if "|" in part:
            url, label = part.rsplit("|", 1)
            targets.append({
                "url": url.strip(),
                "label": label.strip(),
            })
        else:
            targets.append({
                "url": part,
                "label": "CROUS",
            })

    if not targets:
        return DEFAULT_CROUS_TARGETS

    return targets

def send_heartbeat_message(zone_counts, total_found):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    lines = [
        "✅ <b>Bot CROUS toujours actif</b>",
        f"🕒 {now}",
        "",
        "📍 <b>Zones surveillées :</b>",
    ]

    for label, count in zone_counts:
        lines.append(f"• {label} : {count} logement(s) détecté(s)")

    lines.append("")
    lines.append(f"🏠 Total actuel : {total_found}")
    lines.append("🔔 Je notifierai seulement les nouveaux logements.")

    send_telegram_message("\n".join(lines))

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN manquant.")
        return False

    if not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_CHAT_ID manquant.")
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(api_url, data=payload, timeout=20)
        logging.info("Telegram status: %s", response.status_code)

        if response.status_code != 200:
            logging.error("Erreur Telegram: %s", response.text)
            return False

        return True

    except requests.RequestException as error:
        logging.error("Erreur Telegram: %s", error)
        return False


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            return set(data)

        return set()

    except Exception as error:
        logging.warning("Impossible de lire %s: %s", SEEN_FILE, error)
        return set()


def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as file:
            json.dump(sorted(list(seen)), file, ensure_ascii=False, indent=2)

    except Exception as error:
        logging.error("Impossible d'écrire %s: %s", SEEN_FILE, error)


def create_accommodation_id(city_label, title, price, address, surface, link):
    raw = f"{city_label}|{title}|{price}|{address}|{surface}|{link}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_price(text):
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*€", text)
    if match:
        return match.group(0).replace(".", ",")
    return ""


def extract_surface(text):
    match = re.search(
        r"((?:de\s*)?\d+(?:[,.]\d+)?(?:\s*à\s*\d+(?:[,.]\d+)?)?\s*m²)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return ""


# =========================
# SELENIUM
# =========================

def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--lang=fr-FR")
    chrome_options.add_argument(f"user-agent={HEADERS['User-Agent']}")

    driver = webdriver.Chrome(options=chrome_options)
    return driver


def fetch_rendered_page(driver, url):
    logging.info("Ouverture Selenium: %s", url)

    driver.get(url)

    try:
        WebDriverWait(driver, 15).until(
            lambda d: "logement" in d.page_source.lower()
        )
    except Exception:
        logging.warning("Timeout Selenium, récupération quand même du HTML.")

    time.sleep(3)

    html = driver.page_source
    final_url = driver.current_url

    logging.info("URL finale Selenium: %s", final_url)
    logging.info("Taille HTML rendu: %s", len(html))

    return html, final_url


# =========================
# PARSING CROUS
# =========================

def extract_result_count_from_page(html):
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text(" "))

    logging.info("Extrait texte page: %s", page_text[:500])

    patterns = [
        r"(\d+)\s+logement(?:s)?\s+trouv(?:é|e|és|ées)",
        r"(\d+)\s+logement(?:s)?\s+trouve(?:s)?",
        r"(\d+)\s+résultat(?:s)?",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)

        if match:
            return int(match.group(1))

    return 0


def extract_accommodations_from_page(html, page_url, city_label):
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text(" "))

    if "Identification" in page_text and "Mot de passe" in page_text:
        logging.warning("La page semble demander une authentification.")
        return []

    accommodations = {}

    price_nodes = soup.find_all(string=re.compile(r"\d+(?:[,.]\d+)?\s*€"))

    logging.info("Prix détectés dans HTML: %s", len(price_nodes))

    for price_node in price_nodes:
        tag = price_node.parent

        best_block = None
        best_text = ""

        for _ in range(12):
            if tag is None:
                break

            block_text = clean_text(tag.get_text(" "))

            has_price = bool(extract_price(block_text))
            has_surface = bool(extract_surface(block_text))
            good_size = 20 <= len(block_text) <= 2000

            if has_price and good_size:
                best_block = tag
                best_text = block_text

                if has_surface:
                    break

            tag = tag.parent

        if best_block is None:
            continue

        price = extract_price(best_text)
        surface = extract_surface(best_text)

        link = page_url
        a_tag = best_block.find("a", href=True)

        if a_tag:
            link = urljoin(page_url, a_tag["href"])

        lines = [
            clean_text(line)
            for line in best_block.get_text("\n").split("\n")
            if clean_text(line)
        ]

        title = ""
        address = ""

        for line in lines:
            lower_line = line.lower()

            if "€" in line:
                continue

            if "m²" in line:
                continue

            if "logement" in lower_line and "trouvé" in lower_line:
                continue

            if "afficher sur une carte" in lower_line:
                continue

            if "page précédente" in lower_line or "page suivante" in lower_line:
                continue

            if "individuel" in lower_line or "colocation" in lower_line or "couple" in lower_line:
                continue

            if "wc" in lower_line or "douche" in lower_line or "frigo" in lower_line:
                continue

            if "lit simple" in lower_line or "lits simples" in lower_line:
                continue

            if not title:
                title = line
                continue

            if not address:
                address = line
                break

        if not title:
            title = f"Logement CROUS {city_label}"

        accommodation_id = create_accommodation_id(
            city_label=city_label,
            title=title,
            price=price,
            address=address,
            surface=surface,
            link=link,
        )

        accommodations[accommodation_id] = {
            "id": accommodation_id,
            "city_label": city_label,
            "title": title,
            "price": price,
            "surface": surface,
            "address": address,
            "link": link,
            "raw": best_text,
        }

    return list(accommodations.values())


def fetch_all_accommodations(driver, start_url, city_label):
    html, final_url = fetch_rendered_page(driver, start_url)

    page_result_count = extract_result_count_from_page(html)

    accommodations = extract_accommodations_from_page(
        html=html,
        page_url=final_url,
        city_label=city_label,
    )

    unique = {}

    for accommodation in accommodations:
        unique[accommodation["id"]] = accommodation

    return list(unique.values()), page_result_count


# =========================
# MESSAGES TELEGRAM
# =========================

def format_accommodation_message(accommodation):
    city = accommodation["city_label"]
    title = accommodation["title"]
    price = accommodation["price"]
    surface = accommodation["surface"]
    address = accommodation["address"]
    link = accommodation["link"]

    message = (
        "🏠 <b>Logement CROUS disponible</b>\n\n"
        f"📍 <b>Zone :</b> {city}\n"
        f"🏢 <b>Résidence :</b> {title}\n"
    )

    if price:
        message += f"💶 <b>Prix :</b> {price}\n"

    if surface:
        message += f"📐 <b>Surface :</b> {surface}\n"

    if address:
        message += f"📫 <b>Adresse :</b> {address}\n"

    message += f"\n🔗 {link}"

    return message


def send_status_message(zone_counts, total_found, total_new):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    lines = [
        "📊 <b>Vérification CROUS terminée</b>",
        f"🕒 {now}",
        "",
    ]

    for label, count in zone_counts:
        lines.append(f"📍 <b>{label}</b> : {count} logement(s) trouvé(s)")

    lines.append("")
    lines.append(f"🆕 Nouveaux logements envoyés : {total_new}")
    lines.append(f"🏠 Total détecté : {total_found}")

    send_telegram_message("\n".join(lines))


def send_fallback_availability_message(label, found_count, url):
    send_telegram_message(
        "🏠 <b>Logement CROUS disponible</b>\n\n"
        f"📍 <b>Zone :</b> {label}\n"
        f"🔢 <b>Nombre détecté :</b> {found_count}\n\n"
        "Le bot a détecté des logements disponibles sur la page CROUS.\n\n"
        f"🔗 {url}"
    )


# =========================
# CHECK PRINCIPAL
# =========================

def check_once(driver, crous_targets, seen):
    total_found = 0
    total_new = 0
    zone_counts = []

    for target in crous_targets:
        url = target["url"]
        label = target["label"]

        logging.info("Vérification de %s", label)

        try:
            accommodations, page_count = fetch_all_accommodations(driver, url, label)
        except Exception as error:
            logging.exception("Erreur pendant la vérification de %s: %s", label, error)
            send_telegram_message(
                "⚠️ <b>Erreur pendant la vérification CROUS</b>\n\n"
                f"📍 Zone : {label}\n"
                f"Erreur : {error}\n\n"
                f"🔗 {url}"
            )
            continue

        found_count = max(len(accommodations), page_count)

        total_found += found_count
        zone_counts.append((label, found_count))

        logging.info(
            "%s: %s logement(s) trouvé(s), %s détail(s) extrait(s)",
            label,
            found_count,
            len(accommodations),
        )

        for accommodation in accommodations:
            accommodation_id = accommodation["id"]

            if accommodation_id in seen:
                continue

            seen.add(accommodation_id)
            total_new += 1

            if NOTIFY_ALL_AVAILABLE:
                message = format_accommodation_message(accommodation)
                send_telegram_message(message)
                time.sleep(1)

        if found_count > 0 and len(accommodations) == 0:
            fallback_id = hashlib.sha256(
                f"{label}|{url}|{found_count}".encode("utf-8")
            ).hexdigest()

            if fallback_id not in seen:
                seen.add(fallback_id)
                total_new += found_count

                send_fallback_availability_message(
                    label=label,
                    found_count=found_count,
                    url=url,
                )

    save_seen(seen)

    if SEND_STATUS_EVERY_CHECK:
        send_status_message(
            zone_counts=zone_counts,
            total_found=total_found,
            total_new=total_new,
        )

    return total_found, total_new


# =========================
# MAIN
# =========================

def main():
    logging.info("Démarrage du bot CROUS Telegram.")

    crous_targets = parse_crous_urls(CROUS_URLS_RAW)
    seen = load_seen()

    labels = ", ".join([target["label"] for target in crous_targets])

    send_telegram_message(
        "✅ <b>Bot CROUS démarré</b>\n\n"
        f"📍 Zones surveillées : {labels}\n"
        f"⏱️ Vérification toutes les {CHECK_INTERVAL_SECONDS} secondes\n"
        f"⏳ Durée max de cette exécution : {RUN_MINUTES} minute(s)"
    )

    driver = create_driver()

    try:
        end_time = time.time() + RUN_MINUTES * 60

        while True:
            logging.info("Nouvelle boucle de vérification.")

            total_found, total_new = check_once(
                driver=driver,
                crous_targets=crous_targets,
                seen=seen,
            )

            logging.info(
                "Check terminé: %s logements trouvés, %s nouveaux.",
                total_found,
                total_new,
            )

            if time.time() >= end_time:
                logging.info("Fin de la période RUN_MINUTES.")
                break

            time.sleep(CHECK_INTERVAL_SECONDS)

    except Exception as error:
        logging.exception("Erreur inattendue: %s", error)

        send_telegram_message(
            "⚠️ <b>Erreur inattendue dans le bot CROUS</b>\n\n"
            f"{error}"
        )

        raise

    finally:
        driver.quit()
        logging.info("Fin du bot CROUS Telegram.")


if __name__ == "__main__":
    main()
