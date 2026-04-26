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
        wait = WebDriverWait(driver, 15)

        # Step 1: Go directly to login page
        logger.info(f"Going to the login page: {settings.MSE_LOGIN_URL}")
        driver.get(settings.MSE_LOGIN_URL)
        sleep(self.delay)

        # Step 2: Try multiple selectors for the MSE connect button
        logger.info("Choosing the correct authentication method")
        try:
            mse_connect_button = wait.until(
                EC.element_to_be_clickable((By.CLASS_NAME, "loginapp-button"))
            )
            driver.execute_script("arguments[0].click();", mse_connect_button)
            sleep(self.delay)
        except Exception:
            logger.info("loginapp-button not found, trying alternative selectors")
            try:
                mse_connect_button = wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Mon compte')]"))
                )
                driver.execute_script("arguments[0].click();", mse_connect_button)
                sleep(self.delay)
            except Exception:
                logger.info("No intermediate button found, proceeding directly to credentials")

        # Step 3: Input credentials
        logger.info("Inputting credentials")
        try:
            username_input = wait.until(EC.presence_of_element_located((By.NAME, "j_username")))
            password_input = driver.find_element(By.NAME, "j_password")
            username_input.send_keys(self.email)
            password_input.send_keys(self.password)
            logger.info("Submitting the form")
            password_input.send_keys(Keys.RETURN)
            sleep(self.delay)
        except Exception as e:
            logger.error(f"Could not find credential fields: {e}")
            logger.info(f"Current URL: {driver.current_url}")
            logger.info(f"Page source snippet: {driver.page_source[:2000]}")
            raise

        # Step 4: Validate rules
        self._validate_rules(driver)

        # Step 5: Force update auth status
        driver.get("https://trouverunlogement.lescrous.fr/mse/discovery/connect")
        logger.info("Successfully authenticated to the CROUS website")

    def _validate_rules(self, driver: WebDriver) -> None:
        logger.info("Validating the rules of the CROUS website")
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
