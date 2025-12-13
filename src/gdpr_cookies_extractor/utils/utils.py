import os
import re
import json
import logging
from dataclasses import asdict
from typing import List
from urllib.parse import urlparse
from ..analysis.models import SiteAnalysisResult

logger = logging.getLogger(__name__)

def sanitize_filename(url: str) -> str:
    """
    Sanitizes a URL to be used as a safe directory name.
    """
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed_url = urlparse(url)
    # Replace dots with underscores in the netloc for safety
    sanitized = parsed_url.netloc.replace(".", "_")
    return sanitized

def get_site_output_dir(site_url: str) -> str:
    """Constructs the path to the output directory for a specific site."""
    return os.path.join("output", sanitize_filename(site_url))

def save_site_results(results: List[SiteAnalysisResult]):
    """
    Saves the analysis results for a single site's scenarios to a results.json file
    within the site's dedicated output directory.
    """
    if not results:
        return

    # All results in the list should be for the same site
    site_url = results[0].site_url 
    site_dir = get_site_output_dir(site_url)
    os.makedirs(site_dir, exist_ok=True)

    filepath = os.path.join(site_dir, "results.json")
    try:
        # Convert all dataclass objects in the list to dictionaries
        results_dicts = [asdict(r) for r in results]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(results_dicts, f, indent=4, ensure_ascii=False)
        logger.info(f"Saved {len(results)} scenario results for '{site_url}' to '{filepath}'")
    except Exception as e:
        logger.error(f"Failed to save result to {filepath}: {e}")

def create_output_directories():
    """
    Creates the main output directory if it doesn't already exist.
    Site-specific directories are created on-the-fly.
    """
    os.makedirs("output", exist_ok=True)
    logger.info("Ensured main 'output' directory exists.")


def load_llm_config():
    """
    Loads LLM configuration from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('llm', {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("config.json not found or is invalid. Using empty LLM config.")
        return {}

def load_browser_context_config():
    """
    Loads browser context options from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('browser_context_options', {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning(f"Could not load browser_context_options from config.json: {e}. Using empty dict.")
        return {}

def load_user_defined_keywords():
    """
    Loads user-defined keywords from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('search_keywords', {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning(f"Could not load user_defined_keywords from config.json: {e}. Using empty dict.")
        return {}