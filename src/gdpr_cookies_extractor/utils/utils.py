import re
import os
import json
import logging
from dataclasses import asdict
from typing import List
from urllib.parse import urlparse
from ..analysis.models import SiteAnalysisResult

logger = logging.getLogger(__name__)

def sanitize_filename(url: str) -> str:
    """Sanitizes a URL to be used as a valid filename."""
    parsed_url = urlparse(url)
    
    sanitized = re.sub(r'[\/*?:"<>|]', "_", parsed_url.netloc)
    return sanitized

def save_results(results: List[SiteAnalysisResult], timestamp: str):
    """
    Saves the list of result dataclasses to a timestamped JSON file.
    """
    results_dicts = [asdict(result) for result in results]
    logger.debug(f"Data to be serialized: {results_dicts}") 
    filename = f"output/analysis_results_{timestamp}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(results_dicts, f, indent=4, ensure_ascii=False)
    logger.info(f"Analysis complete. Results saved to {filename}")


def create_output_directories():
    """
    Creates the necessary output directories if they don't already exist.
    """
    os.makedirs("output", exist_ok=True)
    os.makedirs("output/dumps", exist_ok=True)
    logger.info("Ensured output directories exist.")


def load_llm_config():
    """
    Loads LLM configuration from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config['llm']
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not load LLM config from config.json: {e}. Using default.")
        return {"model": "llama3"}


def load_browser_context_config():
    """
    Loads browser context options from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('browser_context_options', {})
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
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
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not load user_defined_keywords from config.json: {e}. Using empty dict.")
        return {}


def load_performance_config():
    """
    Loads performance configuration from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('performance', {})
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not load performance_config from config.json: {e}. Using empty dict.")
        return {}
