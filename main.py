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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Sur GitHub Actions, ce n'est pas grave si dotenv n'est pas installé
    pass


# =========================
# CONFIGURATION
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "5"))

SEND_STATUS_EVERY_CHECK = os.getenv("SEND_STATUS_EVERY_CHECK", "false").lower() == "true"
NOTIFY_ALL_AVAILABLE = os.getenv("NOTIFY_ALL_AVAILABLE", "true").lower() == "true"

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
# Si CROUS_URLS n'est pas configuré dans GitHub Secrets
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
    {
        "label": "Lyon",
        "url": "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=4.7718134_45.8082628_4.8983774_45.7073666&locationName=Lyon",
    },
]


# =========================
# OUTILS
# =========================

def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_crous_urls(raw_value):
    """
    Format attendu :
    url|NomVille,url|NomVille,url|NomVille

    Exemple :
    https://...Angers|Angers,https://...Lyon|Lyon
    """

    if not raw_value:
        logging.warning("CROUS_URLS vide. Utilisation des URLs par défaut.")
        return DEFAULT_CROUS_TARGETS

    targets = []

    parts = raw_value.split(",")

    for part in parts:
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


def fetch_crous_page(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )

        logging.info("CROUS status %s pour %s", response.status_code, url)
        logging.info("URL finale: %s", response.url)

        if response.status_code != 200:
            return None, response.status_code, response.url

        return response.text, response.status_code, response.url

    except requests.RequestException as error:
        logging.error("Erreur requête CROUS: %s", error)
        return None, None, url


# =========================
# PARSING CROUS
# =========================

def looks_like_accommodation_block(text):
    lower = text.lower()

    if "€" not in text:
        return False

    if "logements trouvés" in lower:
        return False

    if "afficher sur une carte" in lower:
        return False

    if "lancer une recherche" in lower:
        return False

    if "mon logement" in lower and "résultats de recherche" in lower:
        return False

    if len(text) < 25:
        return False

    return True


def extract_accommodations_from_page(html, page_url, city_label):
    soup = BeautifulSoup(html, "html.parser")

    page_text = clean_text(soup.get_text(" "))

    if "Identification" in page_text and "Mot de passe" in page_text:
        logging.warning("La page semble demander une authentification.")
        return []

    accommodations = {}
    tags = soup.find_all(["article", "li", "div", "section"])

    for tag in tags:
        block_text = clean_text(tag.get_text(" "))

        if not looks_like_accommodation_block(block_text):
            continue

        price = extract_price(block_text)
        surface = extract_surface(block_text)

        if not price:
            continue

        link = ""
        a_tag = tag.find("a", href=True)

        if a_tag:
            link = urljoin(page_url, a_tag["href"])
        else:
            link = page_url

        lines = [
            clean_text(line)
            for line in tag.get_text("\n").split("\n")
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

            if "individuel" in lower_line or "colocation" in lower_line or "couple" in lower_line:
                continue

            if "wc" in lower_line or "douche" in lower_line or "frigo" in lower_line:
                continue

            if not title:
                title = line
                continue

            if not address:
                address = line
                break

        if not title:
            title = block_text[:80]

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
            "raw": block_text,
        }

    return list(accommodations.values())


def get_next_page_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")

    for a_tag in soup.find_all("a", href=True):
        label = clean_text(a_tag.get_text(" ")).lower()

        if "suivant" in label or "page suivante" in label:
            return urljoin(current_url, a_tag["href"])

    return None


def fetch_all_accommodations(start_url, city_label):
    all_accommodations = []
    visited_pages = set()
    current_url = start_url

    for _ in range(5):
        if current_url in visited_pages:
            break

        visited_pages.add(current_url)

        html, status_code, final_url = fetch_crous_page(current_url)

        if html is None:
            send_telegram_message(
                "⚠️ <b>Erreur CROUS</b>\n\n"
                f"📍 Zone : {city_label}\n"
                f"Status : {status_code}\n"
                f"URL : {current_url}"
            )
            break

        accommodations = extract_accommodations_from_page(
            html=html,
            page_url=final_url,
            city_label=city_label,
        )

        all_accommodations.extend(accommodations)

        next_page = get_next_page_url(html, final_url)

        if not next_page:
            break

        current_url = next_page

    unique = {}

    for accommodation in all_accommodations:
        unique[accommodation["id"]] = accommodation

    return list(unique.values())


# =========================
# MESSAGES
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


# =========================
# CHECK PRINCIPAL
# =========================

def check_once(crous_targets, seen):
    total_found = 0
    total_new = 0
    zone_counts = []

    for target in crous_targets:
        url = target["url"]
        label = target["label"]

        logging.info("Vérification de %s", label)

        accommodations = fetch_all_accommodations(url, label)
        found_count = len(accommodations)

        total_found += found_count
        zone_counts.append((label, found_count))

        logging.info("%s: %s logements trouvés", label, found_count)

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

    end_time = time.time() + RUN_MINUTES * 60

    while True:
        try:
            logging.info("Nouvelle boucle de vérification.")

            total_found, total_new = check_once(
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

        except KeyboardInterrupt:
            logging.info("Arrêt manuel.")
            send_telegram_message("🛑 Bot CROUS arrêté manuellement.")
            break

        except Exception as error:
            logging.exception("Erreur inattendue: %s", error)

            send_telegram_message(
                "⚠️ <b>Erreur inattendue dans le bot CROUS</b>\n\n"
                f"{error}"
            )

            time.sleep(CHECK_INTERVAL_SECONDS)

    logging.info("Fin du bot CROUS Telegram.")


if __name__ == "__main__":
    main()
