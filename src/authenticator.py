import logging
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from time import sleep
from src.settings import Settings

settings = Settings()
logger = logging.getLogger(__name__)

class Authenticator:
    def __init__(self, email: str, password: str, delay: int = 3):
        self.email = email
        self.password = password
        self.delay = delay

    def authenticate_driver(self, driver: WebDriver) -> None:
        logger.info("Authenticating to the CROUS website...")
        wait = WebDriverWait(driver, 20)

        # Aller directement sur trouverunlogement qui va rediriger vers login
        logger.info("Going to trouverunlogement to trigger login redirect")
        driver.get("https://trouverunlogement.lescrous.fr/mse/discovery/connect")
        sleep(self.delay)
        logger.info(f"Current URL after redirect: {driver.current_url}")

        # Chercher le champ email avec plusieurs sélecteurs possibles
        logger.info("Looking for email/username field")
        selectors = [
            (By.NAME, "j_username"),
            (By.NAME, "username"),
            (By.ID, "username"),
            (By.ID, "email"),
            (By.XPATH, "//input[@type='email']"),
            (By.XPATH, "//input[@type='text']"),
        ]

        username_input = None
        for by, selector in selectors:
            try:
                username_input = wait.until(EC.presence_of_element_located((by, selector)))
                logger.info(f"Found username field with selector: {by}={selector}")
                break
            except Exception:
                continue

        if not username_input:
            logger.error(f"Could not find username field. URL: {driver.current_url}")
            logger.error(f"Page source: {driver.page_source[:3000]}")
            raise Exception("Username field not found")

        password_input = None
        for by, selector in [(By.NAME, "j_password"), (By.NAME, "password"), (By.ID, "password"), (By.XPATH, "//input[@type='password']")]:
            try:
                password_input = driver.find_element(by, selector)
                logger.info(f"Found password field with selector: {by}={selector}")
                break
            except Exception:
                continue

        if not password_input:
            raise Exception("Password field not found")

        username_input.send_keys(self.email)
        password_input.send_keys(self.password)
        logger.info("Submitting credentials")
        password_input.send_keys(Keys.RETURN)
        sleep(self.delay)

        logger.info(f"URL after login: {driver.current_url}")

        # Validate rules
        self._validate_rules(driver)

        logger.info("Successfully authenticated to the CROUS website")

    def _validate_rules(self, driver: WebDriver) -> None:
        logger.info("Validating rules")
        driver.get("https://trouverunlogement.lescrous.fr/tools/36/rules")
        sleep(self.delay)
        try:
            validate_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.NAME, "searchSubmit"))
            )
            validate_button.click()
            sleep(self.delay)
        except Exception:
            logger.info("Rules validation button not found, skipping")
