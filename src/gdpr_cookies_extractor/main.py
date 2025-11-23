import pandas as pd
import json
import asyncio
import sys
import logging
import re
import os
from playwright.async_api import async_playwright
from datetime import datetime
from urllib.parse import urlparse, urljoin
from typing import List, Dict, Any, Optional
from dataclasses import asdict

# Import relativi (presupponendo la struttura del progetto)
from .utils.logging_setup import *
from .utils.cookie_helpers import simplify_cookies, count_third_party_cookies
from .analysis.scraper import handle_cookie_banner
from .analysis.ollama_providers import OllamaProvider
from .analysis.privacy_analyzers import PrivacyAnalyzer
from .analysis.llm_interface import AbstractLLMClient
from .analysis.models import SiteAnalysisResult

logger = logging.getLogger(__name__)


def sanitize_filename(url: str) -> str:
    """Sanitizes a URL to be used as a valid filename."""
    parsed_url = urlparse(url)
    
    sanitized = re.sub(r'[\/*?:"<>|]', "_", parsed_url.netloc)
    return sanitized


async def process_site_scenario(context, analyzer: PrivacyAnalyzer, site_url: str, scenario: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], scraper_config: Dict[str, Any]) -> SiteAnalysisResult:
    """
    Runs the full analysis for a single site and a single cookie scenario.
    Returns a SiteAnalysisResult object.
    """
    try:
        set_log_context(site_url, scenario)
        logger.info(f"Processing: {site_url} (Scenario: {scenario})")
        
        # Extract scraper params
        max_hops = scraper_config.get('max_hops', 1)
        fan_out = scraper_config.get('fan_out', 1)

        async with await context.new_page() as page:
            # Navigation and Cookie Handling 
            await page.goto(site_url, wait_until="domcontentloaded", timeout=60000)
            await handle_cookie_banner(page, action=scenario)
            await page.wait_for_timeout(3000)  # Give the page time to process the click

            # Get the final URL after potential redirects from navigation or cookie banners
            current_url = page.url
            logger.info(f"Final URL after navigation: {current_url}")

            cookies = await page.context.cookies()
            logger.info(f"[{scenario}] Captured {len(cookies)} cookies for {current_url}.")

            # Cookie Analysis ---
            simplified_cookies = simplify_cookies(cookies)

            logger.debug("Categorizing cookies...")
            cookie_categories = await analyzer.categorize_cookies(simplified_cookies)

            third_party_count = count_third_party_cookies(current_url, cookies)

            # Find Privacy Policy Page
            llm_output, privacy_policy_links = await analyzer.find_privacy_policy(
                context, current_url, site_dump_folder,
                filter_keywords=search_keywords_config.get('privacy_policy', []),
                max_hops=max_hops,
                fan_out=fan_out
            )

            simple_extractor_links = {"privacy_policy": privacy_policy_links}
            analyses_results = {}
            full_privacy_policy_url = None
            
            if llm_output.get("privacy_policy_url"):
                policy_url_path = llm_output.get("privacy_policy_url")
                full_privacy_policy_url = urljoin(current_url, policy_url_path)

                cookie_declaration_task = analyzer.find_cookie_declaration_page(
                    context, 
                    full_privacy_policy_url,
                    site_dump_folder,
                    search_keywords_config=search_keywords_config,
                    max_hops=max_hops,
                    fan_out=fan_out
                )
                data_retention_task = analyzer.find_data_retention_page(
                    context,
                    full_privacy_policy_url,
                    site_dump_folder,
                    search_keywords_config=search_keywords_config,
                    max_hops=max_hops,
                    fan_out=fan_out
                )
                data_deletion_task = analyzer.find_data_deletion_page(
                    context,
                    full_privacy_policy_url,
                    site_dump_folder,
                    search_keywords_config=search_keywords_config,
                    max_hops=max_hops,
                    fan_out=fan_out
                )
                dpo_task = analyzer.find_dpo_page(
                    context,
                    full_privacy_policy_url,
                    site_dump_folder,
                    search_keywords_config=search_keywords_config,
                    max_hops=max_hops,
                    fan_out=fan_out
                )

                results = await asyncio.gather(cookie_declaration_task, data_retention_task, data_deletion_task, dpo_task)
                
                cookie_decl_res, cookie_decl_links = results[0]
                data_retention_res, data_retention_links = results[1]
                data_deletion_res, data_deletion_links = results[2]
                dpo_res, dpo_links = results[3]

                simple_extractor_links["cookie_declaration"] = cookie_decl_links
                simple_extractor_links["data_retention"] = data_retention_links
                simple_extractor_links["data_deletion"] = data_deletion_links
                simple_extractor_links["dpo"] = dpo_links
                
                # Collect results into the extensible dictionary
                analyses_results = {
                    "cookie_declaration": cookie_decl_res,
                    "data_retention": data_retention_res,
                    "data_deletion": data_deletion_res,
                    "dpo": dpo_res
                }
            
            # Format Success Result ---
            return SiteAnalysisResult.from_outputs(
                site_url=current_url,
                scenario=scenario,
                cookies=cookies,
                cookie_categories=cookie_categories,
                third_party_count=third_party_count,
                llm_output=llm_output,
                privacy_policy_url=full_privacy_policy_url,
                simple_extractor_links=simple_extractor_links,
                **analyses_results
            )

    except Exception as e:
        logger.error(f"FATAL Error processing {site_url} ('{scenario}'): {e}", exc_info=True)
        return SiteAnalysisResult.from_exception(site_url, scenario, e)
    finally:
        clear_log_context()
        if context:
            await context.close()

async def run_all_analyses(sites_df: pd.DataFrame, analyzer: PrivacyAnalyzer, browser, timestamp: str, search_keywords_config: Dict[str, List[str]], scraper_config: Dict[str, Any]) -> List[SiteAnalysisResult]:
    """
    Creates and runs all analysis tasks concurrently.
    """
    tasks = []
    scenarios = ["accept"]
    base_dump_dir = f"output/dumps/analysis_results_{timestamp}"


    for index, row in sites_df.iterrows():
        site_url = row['website_url']
        parsed_url = urlparse(site_url)
        if not parsed_url.scheme:
            site_url = "https://" + site_url

        site_dump_folder = os.path.join(base_dump_dir, sanitize_filename(site_url))
            
        for scenario in scenarios:
            # Create a new context for each task to ensure isolation
            context = await browser.new_context(
                locale='it-IT',
                timezone_id='Europe/Rome',
                geolocation={ "longitude": 12.4964, "latitude": 41.9028 },
                permissions=['geolocation'],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.75 Safari/537.36"
            )
            tasks.append(
                process_site_scenario(context, analyzer, site_url, scenario, site_dump_folder, search_keywords_config, scraper_config)
            )
    
    results = await asyncio.gather(*tasks)
    return results


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


def load_scraper_config():
    """
    Loads scraper configuration from config.json.
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config['scraper']
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not load scraper config from config.json: {e}. Using default.")
        return {"max_hops": 1, "fan_out": 1}


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


async def gdpr_analysis(sites_df):
    """
    Orchestrates the setup, execution, and saving of the analysis.
    """
    llm_config = load_llm_config()
    scraper_config = load_scraper_config()
    search_keywords_config = load_user_defined_keywords()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    llm_provider = OllamaProvider(model=llm_config.get('model', 'llama3'))
    analyzer = PrivacyAnalyzer(
        llm_client=llm_provider,
        timestamp=timestamp
    )
    
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        all_results = await run_all_analyses(sites_df, analyzer, browser, timestamp, search_keywords_config, scraper_config)
        
        await browser.close()
    
    save_results(all_results, timestamp)


def main():
    setup_logging()
    create_output_directories()
    
    logger.info("Starting GDPR Cookie Analysis...")

    if len(sys.argv) > 1:
        site_url_from_cli = sys.argv[1]
        logger.info(f"Processing single URL from command line: {site_url_from_cli}")
        sites_df = pd.DataFrame([{'website_url': site_url_from_cli}])
    else:
        try:
            logger.info("Loading URLs from sites.csv...")
            sites_df = pd.read_csv("sites.csv", header=None, names=['index_col', 'website_url'])
            sites_df = sites_df.drop(columns=['index_col'])
            logger.info(f"Loaded {len(sites_df)} sites from CSV.")
        except FileNotFoundError:
            logger.error("No URL provided and 'sites.csv' file not found.")
            logger.error("Usage: poetry run main <your_url> OR create 'sites.csv'")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reading sites.csv: {e}")
            sys.exit(1)

    asyncio.run(gdpr_analysis(sites_df))


if __name__ == "__main__":
    main()
