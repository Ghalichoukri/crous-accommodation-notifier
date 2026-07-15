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
from dotenv import load_dotenv


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
CROUS_URLS_RAW = os.getenv("CROUS_URLS", "")
SEND_STATUS_EVERY_CHECK = os.getenv("SEND_STATUS_EVERY_CHECK", "false").lower() == "true"

SEEN_FILE = "seen_accommodations.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def parse_crous_urls(raw_value):
    """
    Format attendu dans .env :
    url|NomVille,url|NomVille,url|NomVille

    Exemple :
    https://...Angers|Angers,https://...Tours|Tours
    """
    urls = []

    if not raw_value.strip():
        raise ValueError("La variable CROUS_URLS est vide dans le fichier .env")

    parts = raw_value.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "|" in part:
            url, label = part.rsplit("|", 1)
            urls.append({
                "url": url.strip(),
                "label": label.strip(),
            })
        else:
            urls.append({
                "url": part,
                "label": "CROUS",
            })

    return urls


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Token Telegram ou chat_id manquant.")
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
        logging.error("Impossible d'envoyer le message Telegram: %s", error)
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


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def create_accommodation_id(title, price, address, surface, city_label):
    raw = f"{city_label}|{title}|{price}|{address}|{surface}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_price(text):
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*€", text)
    if match:
        return match.group(0).replace(".", ",")
    return ""


def extract_surface(text):
    match = re.search(r"((?:de\s*)?\d+(?:[,.]\d+)?(?:\s*à\s*\d+(?:[,.]\d+)?)?\s*m²)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def extract_accommodations_from_page(html, page_url, city_label):
    soup = BeautifulSoup(html, "html.parser")

    text_page = clean_text(soup.get_text(" "))

    if "Identification" in text_page and "Mot de passe" in text_page:
        logging.warning("La page semble demander une authentification.")
        return []

    candidates = []

    # Stratégie robuste :
    # On cherche les blocs qui contiennent un prix en euros.
    # Le site CROUS peut changer ses classes CSS, donc on ne dépend pas trop des classes.
    for tag in soup.find_all(["article", "li", "div"]):
        block_text = clean_text(tag.get_text(" "))

        if "€" not in block_text:
            continue

        if len(block_text) < 20:
            continue

        if "logements trouvés" in block_text.lower():
            continue

        price = extract_price(block_text)
        surface = extract_surface(block_text)

        if not price:
            continue

        # Essayer de récupérer le lien du détail logement
        link = ""
        a_tag = tag.find("a", href=True)
        if a_tag:
            link = urljoin(page_url, a_tag["href"])

        lines = [clean_text(x) for x in tag.get_text("\n").split("\n")]
        lines = [x for x in lines if x]

        # Titre probable : première ligne non prix
        title = ""
        address = ""

        for line in lines:
            if "€" in line:
                continue
            if "m²" in line:
                continue
            if not title:
                title = line
            elif not address:
                address = line
                break

        if not title:
            title = block_text[:80]

        if not address:
            address = ""

        accommodation_id = create_accommodation_id(
            title=title,
            price=price,
            address=address,
            surface=surface,
            city_label=city_label,
        )

        accommodation = {
            "id": accommodation_id,
            "city_label": city_label,
            "title": title,
            "price": price,
            "surface": surface,
            "address": address,
            "link": link or page_url,
            "raw": block_text,
        }

        # Éviter les doublons extraits plusieurs fois à cause des div imbriquées
        if accommodation_id not in [item["id"] for item in candidates]:
            candidates.append(accommodation)

    return candidates


def get_next_page_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")

    # Chercher un lien "Suivant" ou page suivante
    for a_tag in soup.find_all("a", href=True):
        label = clean_text(a_tag.get_text(" ")).lower()

        if "suivant" in label or "page suivante" in label:
            return urljoin(current_url, a_tag["href"])

    return None


def fetch_crous_page(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)

        logging.info("CROUS status %s pour %s", response.status_code, url)
        logging.info("URL finale: %s", response.url)

        if response.status_code != 200:
            logging.error("Erreur HTTP CROUS %s", response.status_code)
            return None, response.status_code

        return response.text, response.status_code

    except requests.RequestException as error:
        logging.error("Erreur requête CROUS: %s", error)
        return None, None


def fetch_all_accommodations(start_url, city_label):
    all_accommodations = []
    visited_pages = set()
    current_url = start_url

    # Limite de sécurité : maximum 5 pages par zone
    for _ in range(5):
        if current_url in visited_pages:
            break

        visited_pages.add(current_url)

        html, status_code = fetch_crous_page(current_url)

        if html is None:
            send_telegram_message(
                f"⚠️ Erreur CROUS pour <b>{city_label}</b>\n"
                f"URL: {current_url}\n"
                f"Status: {status_code}"
            )
            break

        accommodations = extract_accommodations_from_page(
            html=html,
            page_url=current_url,
            city_label=city_label,
        )

        all_accommodations.extend(accommodations)

        next_page = get_next_page_url(html, current_url)

        if not next_page:
            break

        current_url = next_page

    # Dédupliquer
    unique = {}
    for accommodation in all_accommodations:
        unique[accommodation["id"]] = accommodation

    return list(unique.values())


def format_accommodation_message(accommodation):
    title = accommodation["title"]
    city = accommodation["city_label"]
    price = accommodation["price"]
    surface = accommodation["surface"]
    address = accommodation["address"]
    link = accommodation["link"]

    message = (
        "🏠 <b>Nouveau logement CROUS détecté</b>\n\n"
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


def check_once(crous_targets, seen, first_run=False):
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

            if first_run:
                continue

            total_new += 1

            message = format_accommodation_message(accommodation)
            send_telegram_message(message)

            time.sleep(1)

    save_seen(seen)

    if SEND_STATUS_EVERY_CHECK:
        lines = [
            "📊 <b>Test CROUS terminé</b>",
            "",
        ]

        for label, count in zone_counts:
            lines.append(f"📍 <b>{label}</b> : {count} logement(s) trouvé(s)")

        lines.append("")
        lines.append(f"🆕 Nouveaux logements : {total_new}")
        lines.append(f"🏠 Total détecté : {total_found}")

        send_telegram_message("\n".join(lines))

    return total_found, total_new

def main():
    logging.info("Démarrage du bot CROUS Telegram.")

    crous_targets = parse_crous_urls(CROUS_URLS_RAW)
    seen = load_seen()

    send_telegram_message(
        "✅ Bot CROUS démarré.\n"
        f"Surveillance de {len(crous_targets)} zone(s).\n"
        f"Intervalle: {CHECK_INTERVAL_SECONDS} secondes."
    )

    first_run = len(seen) == 0

    if first_run:
        logging.info("Premier lancement: initialisation du cache sans notification massive.")
        total_found, total_new = check_once(crous_targets, seen, first_run=True)

        send_telegram_message(
            "ℹ️ Initialisation terminée.\n"
            f"{total_found} logement(s) actuellement détecté(s).\n"
            "Les prochaines nouveautés seront envoyées en notification."
        )

    while True:
        try:
            logging.info("Nouvelle boucle de vérification.")

            total_found, total_new = check_once(
                crous_targets=crous_targets,
                seen=seen,
                first_run=False,
            )

            logging.info(
                "Check terminé: %s logements trouvés, %s nouveaux.",
                total_found,
                total_new,
            )

            time.sleep(CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logging.info("Arrêt demandé par l'utilisateur.")
            send_telegram_message("🛑 Bot CROUS arrêté manuellement.")
            break

        except Exception as error:
            logging.exception("Erreur inattendue: %s", error)
            send_telegram_message(
                f"⚠️ Erreur inattendue dans le bot CROUS:\n{error}"
            )
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
