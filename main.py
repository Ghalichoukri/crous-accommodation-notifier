import argparse
import logging
import os
from typing import List
import telepot
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromiumService
from selenium.webdriver.chrome.webdriver import WebDriver
from src.authenticator import Authenticator
from src.parser import Parser
from src.models import UserConf
from src.notification_builder import NotificationBuilder
from src.settings import Settings
from src.telegram_notifier import TelegramNotifier

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.INFO,
)
logger = logging.getLogger("accommodation_notifier")

def load_users_conf(settings: Settings) -> List[UserConf]:
    return [
        UserConf(
            conf_title="Me",
            telegram_id=settings.MY_TELEGRAM_ID,
            search_url=settings.SEARCH_URL,
            ignored_ids=[],
        )
    ]

def create_driver(headless: bool = True) -> WebDriver:
    chrome_options = Options()
    if headless:
        logging.info("Running in headless mode")
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
    else:
        logging.info("Running in non-headless mode")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    return webdriver.Chrome(
        options=chrome_options,
        service=ChromiumService(
            executable_path=os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"),
        ),
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the script in headless mode or not."
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run the script without headless mode",
    )
    args = parser.parse_args()
    settings = Settings()
    bot = telepot.Bot(token=settings.TELEGRAM_BOT_TOKEN)
    bot.getMe()
    user_confs = load_users_conf(settings)
    driver = create_driver(headless=not args.no_headless)
    Authenticator(settings.MSE_EMAIL, settings.MSE_PASSWORD).authenticate_driver(driver)
    parser = Parser(driver)
    notification_builder = NotificationBuilder()
    notifier = TelegramNotifier(bot)
    for conf in user_confs:
        logging.info(f"Handling configuration : {conf}")
        search_results = parser.get_accommodations(conf.search_url)
        notification = notification_builder.search_results_notification(search_results)
        if notification:
            notifier.send_notification(conf.telegram_id, notification)
    driver.quit()
