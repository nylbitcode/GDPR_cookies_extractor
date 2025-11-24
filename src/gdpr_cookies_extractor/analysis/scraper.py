import logging
import json
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

def load_selectors_from_config():
    """Loads cookie banner selectors from config.json."""
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config['scraper']['cookie_banners']
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error loading selectors from config.json: {e}")
        # Fallback to default selectors if config is invalid or not found
        return {
            "accept_selectors": [
                "text=Accept", "text=Accept All", "text=OK",
                "role=button[name='Accept']", "role=button[name='Accept All']", "role=button[name='OK']"
            ],
            "reject_selectors": [
                "text=Reject", "text=Reject All", "text=Deny",
                "role=button[name='Reject']", "role=button[name='Reject All']", "role=button[name='Deny']"
            ],
            "only_essential_selectors": [
                "text=Only Essential", "text=Essential Cookies", "text=Accept Essential",
                "role=button[name='Only Essential']", "role=button[name='Essential Cookies']", "role=button[name='Accept Essential']"
            ]
        }

async def handle_cookie_banner(page, action="accept", click: bool = True):
    """
    Finds and optionally clicks the cookie banner button based on the desired action.
    If 'click' is False, it only checks for the button's visibility.
    """
    selectors_config = load_selectors_from_config()
    accept_selectors = selectors_config.get("accept_selectors", [])
    reject_selectors = selectors_config.get("reject_selectors", [])
    only_essential_selectors = selectors_config.get("only_essential_selectors", [])

    if action == "accept":
        target_selectors = accept_selectors
    elif action == "reject":
        target_selectors = reject_selectors
    elif action == "only_essential":
        target_selectors = only_essential_selectors
    else:
        target_selectors = []

    for selector in target_selectors:
        try:
            button = page.locator(selector)
            
            if await button.is_visible(timeout=5000):
                if click:
                    logger.info(f"Clicking '{action}' button with selector: {selector}")
                    await button.click()
                    await page.wait_for_timeout(2000)  # Give the page time to process the click
                else:
                    logger.info(f"Found '{action}' button with selector: {selector} (no click)")
                return True
        except Exception:
            continue
    
    logger.info(f"No '{action}' button found for this site.")
    return False

def simple_extractor(html_page):
    """
    A simple rule-based function to find privacy-related links using BeautifulSoup.
    """
    soup = BeautifulSoup(html_page, "html.parser")

    privacy_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        if "privacy" in href or "privacy" in text:
            privacy_links.append(a["href"])

    privacy_links = list(set(privacy_links))

    logger.info(f"simple_extractor found {len(privacy_links)} privacy-related links.")
    return privacy_links

async def get_page_content(page, url):
    """
    Navigates to a URL and returns the complete HTML content after JavaScript execution.
    """
    # The page.content() method returns the full HTML source after JavaScript has run,
    # which is ideal for scraping dynamic content.
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    html_content = await page.content()
    return html_content