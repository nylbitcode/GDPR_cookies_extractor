import logging
from typing import List, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def simplify_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Simplifies a list of Playwright cookie objects into a list of dictionaries
    containing only the name and domain of each cookie.
    """
    logger.debug("Simplifying cookies...")
    simplified_cookies = []
    for c in cookies:
        simplified_cookies.append({"name": c.get("name"), "domain": c.get("domain")})
    logger.debug(f"Cookies simplified. Found {len(simplified_cookies)} cookies.")
    return simplified_cookies

def count_third_party_cookies(site_url: str, cookies: List[Dict[str, Any]]) -> int:
    """
    Counts the number of third-party cookies based on the site's domain.
    A cookie is considered third-party if its domain does not match the site's base domain.
    """
    logger.debug("Parsing base domain for third-party cookie count...")
    try:
        base_domain = urlparse(site_url).netloc.replace('www.', '')
        
        comparable_base_domain = base_domain.replace('.', '')

        logger.debug("Counting third-party cookies...")
        third_party_count = 0
        for c in cookies:
            cookie_domain = c.get('domain')
            if cookie_domain:
                comparable_cookie_domain = cookie_domain.replace('.', '')
                if not comparable_cookie_domain.endswith(comparable_base_domain):
                    third_party_count += 1
        
        logger.info(f"Found {third_party_count} third-party cookies.")
        return third_party_count
    except Exception as e:
        logger.error(f"Could not count third-party cookies for {site_url}: {e}")
        return 0 