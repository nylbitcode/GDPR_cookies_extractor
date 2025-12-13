import pandas as pd
import json
import asyncio
import sys
import logging
import re
import os
import argparse
from playwright.async_api import async_playwright
from datetime import datetime
from urllib.parse import urlparse, urljoin
from typing import List, Dict, Any, Optional
from dataclasses import asdict

# Relative imports
from .utils.logging_setup import *
from .utils.cookie_helpers import simplify_cookies, count_third_party_cookies
from .utils.utils import (
    get_site_output_dir,
    save_site_results,
    create_output_directories,
    load_llm_config,
    load_browser_context_config,
    load_user_defined_keywords
)
from .analysis.scraper import handle_cookie_banner
from .analysis.ollama_providers import OllamaProvider
from .analysis.privacy_analyzers import PrivacyAnalyzer
from .analysis.llm_interface import AbstractLLMClient
from .analysis.models import SiteAnalysisResult

logger = logging.getLogger(__name__)


async def process_site_scenario(context, analyzer: PrivacyAnalyzer, site_url: str, scenario: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]]) -> SiteAnalysisResult:
    """
    Runs the full analysis for a single site and a single cookie scenario.
    Returns a SiteAnalysisResult object.
    """
    page = None
    try:
        set_log_context(site_url, scenario)
        logger.info(f"Processing: {site_url} (Scenario: {scenario})")
        
        page = await context.new_page()
        
        # Navigation and Cookie Handling 
        await page.goto(site_url, wait_until="domcontentloaded", timeout=60000)
        
        # For "initial" scan, we don't click anything. For others, we click.
        if scenario != "initial":
            await handle_cookie_banner(page, action=scenario, click=True)
        
        await page.wait_for_timeout(3000)

        # Get the final URL after potential redirects
        current_url = page.url
        logger.info(f"Final URL after navigation/action: {current_url}")

        cookies = await page.context.cookies()
        logger.info(f"[{scenario}] Captured {len(cookies)} cookies for {current_url}.")

        # --- Main Analysis Logic ---
        simplified_cookies = simplify_cookies(cookies)
        cookie_categories = await analyzer.categorize_cookies(simplified_cookies)
        third_party_count = count_third_party_cookies(current_url, cookies)

        llm_output, privacy_policy_links = await analyzer.find_privacy_policy(
            context, current_url, site_dump_folder,
            filter_keywords=search_keywords_config.get('privacy_policy', []),
        )

        simple_extractor_links = {"privacy_policy": privacy_policy_links}
        full_privacy_policy_url = None
        
        cookie_decl_res, data_retention_res, data_deletion_res, dpo_res = None, None, None, None

        if llm_output.get("privacy_policy_url"):
            policy_url_path = llm_output.get("privacy_policy_url")
            full_privacy_policy_url = urljoin(current_url, policy_url_path)

            # Gather all sub-analysis tasks
            cookie_declaration_task = analyzer.find_cookie_declaration_page(context, full_privacy_policy_url, site_dump_folder, search_keywords_config)
            data_retention_task = analyzer.find_data_retention_page(context, full_privacy_policy_url, site_dump_folder, search_keywords_config)
            data_deletion_task = analyzer.find_data_deletion_page(context, full_privacy_policy_url, site_dump_folder, search_keywords_config)
            dpo_task = analyzer.find_dpo_page(context, full_privacy_policy_url, site_dump_folder, search_keywords_config)
            
            # Run sub-analyses concurrently
            results = await asyncio.gather(cookie_declaration_task, data_retention_task, data_deletion_task, dpo_task)
            
            # Process results
            cookie_decl_res, cookie_decl_links = results[0]
            data_retention_res, data_retention_links = results[1]
            data_deletion_res, data_deletion_links = results[2]
            dpo_res, dpo_links = results[3]

            simple_extractor_links["cookie_declaration"] = cookie_decl_links
            simple_extractor_links["data_retention"] = data_retention_links
            simple_extractor_links["data_deletion"] = data_deletion_links
            simple_extractor_links["dpo"] = dpo_links

        return SiteAnalysisResult.from_outputs(
            site_url=current_url,
            scenario=scenario,
            cookies=cookies,
            cookie_categories=cookie_categories,
            third_party_count=third_party_count,
            llm_output=llm_output,
            privacy_policy_url=full_privacy_policy_url,
            simple_extractor_links=simple_extractor_links,
            cookie_declaration=cookie_decl_res,
            data_retention=data_retention_res,
            data_deletion=data_deletion_res,
            dpo=dpo_res
        )

    except Exception as e:
        logger.error(f"FATAL Error processing {site_url} ('{scenario}'): {e}", exc_info=True)
        return SiteAnalysisResult.from_exception(site_url, scenario, e)
    finally:
        if page and not page.is_closed():
            await page.close()


async def run_sequential_analyses(sites_df: pd.DataFrame, analyzer: PrivacyAnalyzer, browser, search_keywords_config: Dict[str, List[str]], browser_context_config: Dict[str, Any]):
    """
    Orchestrates site analysis sequentially, site by site.
    """
    for index, row in sites_df.iterrows():
        site_url = row['website_url']
        if not urlparse(site_url).scheme:
            site_url = "https://" + site_url
        
        site_output_dir = get_site_output_dir(site_url)
        site_dump_folder = os.path.join(site_output_dir, "dumps")
        os.makedirs(site_dump_folder, exist_ok=True)
        
        # Detection Phase 
        logger.info(f"Detecting available cookie scenarios for {site_url}...")
        scenarios_to_run = ["initial"]
        
        try:
            async with await browser.new_context(**browser_context_config) as detection_context:
                page = await detection_context.new_page()
                await page.goto(site_url, wait_until="domcontentloaded", timeout=60000)
                
                possible_scenarios = ["accept", "only_essential"]
                for scenario_option in possible_scenarios:
                    if await handle_cookie_banner(page, action=scenario_option, click=False):
                        scenarios_to_run.append(scenario_option)
                
                logger.info(f"Scenarios to run for {site_url}: {scenarios_to_run}")
                await page.close()
        except Exception as e:
            logger.error(f"Failed to detect scenarios for {site_url}: {e}. Will only run 'initial'.")

        # Execution Phase
        site_scenario_results = []
        for scenario in scenarios_to_run:
            async with await browser.new_context(**browser_context_config) as analysis_context:
                result = await process_site_scenario(
                    analysis_context, analyzer, site_url, scenario, site_dump_folder, 
                    search_keywords_config
                )
                site_scenario_results.append(result)
        
        # Save results for the site after all its scenarios are processed
        save_site_results(site_scenario_results)
    
    logger.info("All sites have been processed.")


async def gdpr_analysis(sites_df: pd.DataFrame, args: argparse.Namespace):
    """
    Orchestrates the setup, execution, and saving of the analysis.
    """
    llm_config = load_llm_config()
    search_keywords_config = load_user_defined_keywords()
    browser_context_config = load_browser_context_config()
    
    llm_provider = OllamaProvider(model=llm_config.get('model', 'llama3'))
    analyzer = PrivacyAnalyzer(
        llm_client=llm_provider
    )
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-infobars',
                '--window-position=0,0',
                '--ignore-certificate-errors',
                '--ignore-certificate-errors-spki-list',
            ]
        )
        
        await run_sequential_analyses(sites_df, analyzer, browser, search_keywords_config, browser_context_config)
        
        await browser.close()


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GDPR-compliant cookie and privacy policy analyzer.")
    
    # Input source arguments
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", type=str, help="A single URL to analyze.")
    group.add_argument("--file", type=str, default="sites.csv", nargs='?', help="Path to a CSV file containing a list of URLs to analyze. Defaults to 'sites.csv'.")
    
    return parser.parse_args()


def main():
    setup_logging()
    create_output_directories()
    args = parse_args()
    
    logger.info("Starting GDPR Analysis...")

    if args.url:
        logger.info(f"Processing single URL from command line: {args.url}")
        sites_df = pd.DataFrame([{'website_url': args.url}])
    else: # args.file is used
        try:
            logger.info(f"Loading URLs from {args.file}...")
            sites_df = pd.read_csv(args.file)
            if 'website_url' not in sites_df.columns:
                if len(sites_df.columns) == 1:
                    sites_df.columns = ['website_url']
                else:
                    raise ValueError("CSV file must have a 'website_url' column or be a single-column file.")
            logger.info(f"Loaded {len(sites_df)} sites from CSV.")
        except FileNotFoundError:
            logger.error(f"Input file '{args.file}' not found.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reading {args.file}: {e}")
            sys.exit(1)

    asyncio.run(gdpr_analysis(sites_df, args))


if __name__ == "__main__":
    main()