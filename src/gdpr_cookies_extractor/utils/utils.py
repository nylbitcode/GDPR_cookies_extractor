import os
import re
import json
import logging
from dataclasses import asdict
from typing import List, Optional, Dict
from urllib.parse import urlparse
from ..analysis.models import (
    SiteAnalysisResult, Analyses, CookieDeclarationAnalysis, 
    DataRetentionAnalysis, DataDeletionAnalysis, DPOAnalysis
)

logger = logging.getLogger(__name__)

def sanitize_filename(website_url: str) -> str:
    """
    Sanitizes a URL to be used as a safe directory name.
    """
    if not website_url.startswith(('http://', 'https://')):
        website_url = 'https://' + website_url
    parsed_url = urlparse(website_url)
    netloc = parsed_url.netloc
    # Remove 'www.' prefix to treat www.example.com and example.com as the same site
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Replace remaining dots with underscores for safety
    sanitized = netloc.replace(".", "_")
    return sanitized

def get_site_output_dir(website_url: str) -> str:
    """Constructs the path to the output directory for a specific site."""
    return os.path.join("output", sanitize_filename(website_url))

def _reconstruct_dataclasses(data: Dict) -> SiteAnalysisResult:
    """Helper to reconstruct nested dataclasses from a dictionary."""
    # Reconstruct the 'Analyses' object
    analyses_data = data.get('analyses', {})
    if analyses_data:
        for key, value in analyses_data.items():
            if value:
                if key == 'cookie_declaration':
                    analyses_data[key] = CookieDeclarationAnalysis(**value)
                elif key == 'data_retention':
                    analyses_data[key] = DataRetentionAnalysis(**value)
                elif key == 'data_deletion':
                    analyses_data[key] = DataDeletionAnalysis(**value)
                elif key == 'dpo':
                    analyses_data[key] = DPOAnalysis(**value)
        data['analyses'] = Analyses(**analyses_data)

    return SiteAnalysisResult(**data)

def load_site_results(website_url: str) -> List[SiteAnalysisResult]:
    """
    Loads all existing analysis results for a given site URL by reconstructing the dataclasses.
    """
    site_dir = get_site_output_dir(website_url)
    filepath = os.path.join(site_dir, "results.json")
    
    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            all_data = json.load(f)
        
        reconstructed_results = [_reconstruct_dataclasses(data) for data in all_data]
        logger.info(f"Loaded {len(reconstructed_results)} existing scenario results for {website_url}.")
        return reconstructed_results
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.error(f"Could not load or parse result file {filepath}: {e}")
        return []

def save_site_results(results_to_save: List[SiteAnalysisResult]):
    """
    Saves/updates the analysis results for a single site. It loads existing results
    and updates them with the new ones based on the scenario.
    """
    if not results_to_save:
        return

    website_url = results_to_save[0].website_url
    site_dir = get_site_output_dir(website_url)
    os.makedirs(site_dir, exist_ok=True)
    filepath = os.path.join(site_dir, "results.json")

    # Load existing results
    existing_results_map = {res.scenario: res for res in load_site_results(website_url)}

    # Update with new results
    for new_result in results_to_save:
        existing_results_map[new_result.scenario] = new_result
    
    # Get the final list of results to save
    final_results_list = list(existing_results_map.values())

    try:
        results_dicts = [asdict(r) for r in final_results_list]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(results_dicts, f, indent=4, ensure_ascii=False)
        logger.info(f"Saved/Updated {len(final_results_list)} scenario results for '{website_url}' to '{filepath}'")
    except Exception as e:
        logger.error(f"Failed to save result to {filepath}: {e}")

def create_output_directories():
    """
    Creates the main output directory if it doesn't already exist.
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