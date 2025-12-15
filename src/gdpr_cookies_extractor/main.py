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
from typing import List, Dict, Any

# Relative imports
from .utils.logging_setup import *
from .utils.cookie_helpers import simplify_cookies, count_third_party_cookies
from .utils.utils import (
    get_site_output_dir,
    save_site_results,
    create_output_directories,
    load_llm_config,
    load_browser_context_config,
    load_user_defined_keywords,
    load_site_results
)
from .analysis.scraper import handle_cookie_banner
from .analysis.ollama_providers import OllamaProvider
from .analysis.privacy_analyzers import PrivacyAnalyzer
from .analysis.models import SiteAnalysisResult

logger = logging.getLogger(__name__)


async def process_site_scenario(
    context, 
    analyzer: PrivacyAnalyzer, 
    result: SiteAnalysisResult,
    site_dump_folder: str, 
    search_keywords_config: Dict[str, List[str]],
    tasks: List[str]
) -> SiteAnalysisResult:
    """
    Runs the selected analysis tasks for a single site and a single cookie scenario.
    This function modifies the 'result' object in place.
    """
    page = None
    try:
        set_log_context(result.website_url, result.scenario)
        logger.info(f"Processing tasks {tasks} for {result.website_url} (Scenario: {result.scenario})")

        page = await context.new_page()
        
        # --- Task: Analyze Cookies ('cookies') ---
        if 'cookies' in tasks and not result.cookies:
            logger.info("Running task: 'cookies'")
            await page.goto(result.website_url, wait_until="domcontentloaded", timeout=60000)
            
            if result.scenario != "initial":
                await handle_cookie_banner(page, action=result.scenario, click=True)
            
            await page.wait_for_timeout(3000)

            result.website_url = page.url # Update URL in case of redirects
            
            cookies = await page.context.cookies()
            logger.info(f"[{result.scenario}] Captured {len(cookies)} cookies.")
            
            result.cookies = cookies
            if cookies:
                result.simplified_cookies = simplify_cookies(cookies)
                result.cookie_categories = await analyzer.categorize_cookies(result.simplified_cookies)
                result.third_party_cookie_count = count_third_party_cookies(result.website_url, cookies)
        elif 'cookies' in tasks:
            logger.info("Skipping task 'cookies': already completed.")

        # --- Dependency Check for all subsequent tasks ---
        # If any sub-analysis is requested, we must have the privacy policy URL.
        sub_analysis_tasks = {'find-cd', 'find-dpo', 'find-delete', 'find-retention'}
        if any(task in tasks for task in sub_analysis_tasks) and not result.privacy_policy_url:
            # If 'find-pp' was not explicitly requested, add it to the tasks to run.
            if 'find-pp' not in tasks:
                tasks.append('find-pp')
            logger.info("Dependency detected: 'find-pp' task must be run first.")

        # --- Task: Find Privacy Policy ('find-pp') ---
        if 'find-pp' in tasks and not result.privacy_policy_url:
            logger.info("Running task: 'find-pp'")
            # Ensure we are on the main page before searching for the privacy policy
            if not page.url or urlparse(page.url).netloc != urlparse(result.website_url).netloc:
                 await page.goto(result.website_url, wait_until="domcontentloaded", timeout=60000)

            llm_output, _ = await analyzer.find_privacy_policy(
                context, page.url, site_dump_folder,
                filter_keywords=search_keywords_config.get('privacy_policy', []),
            )
            result.update_llm_output(llm_output)
            if llm_output.get("privacy_policy_url"):
                result.privacy_policy_url = urljoin(page.url, llm_output.get("privacy_policy_url"))
        elif 'find-pp' in tasks:
            logger.info("Skipping task 'find-pp': already completed.")

        # --- Sub-Analyses Tasks ---
        if result.privacy_policy_url:
            if 'find-cd' in tasks and not result.analyses.cookie_declaration:
                logger.info("Running task: 'find-cd'")
                res, _ = await analyzer.find_cookie_declaration_page(context, result.privacy_policy_url, site_dump_folder, search_keywords_config)
                result.analyses.cookie_declaration = res
            elif 'find-cd' in tasks:
                logger.info("Skipping task 'find-cd': already completed.")

            if 'find-retention' in tasks and not result.analyses.data_retention:
                logger.info("Running task: 'find-retention'")
                res, _ = await analyzer.find_data_retention_page(context, result.privacy_policy_url, site_dump_folder, search_keywords_config)
                result.analyses.data_retention = res
            elif 'find-retention' in tasks:
                logger.info("Skipping task 'find-retention': already completed.")
                
            if 'find-delete' in tasks and not result.analyses.data_deletion:
                logger.info("Running task: 'find-delete'")
                res, _ = await analyzer.find_data_deletion_page(context, result.privacy_policy_url, site_dump_folder, search_keywords_config)
                result.analyses.data_deletion = res
            elif 'find-delete' in tasks:
                logger.info("Skipping task 'find-delete': already completed.")

            if 'find-dpo' in tasks and not result.analyses.dpo:
                logger.info("Running task: 'find-dpo'")
                res, _ = await analyzer.find_dpo_page(context, result.privacy_policy_url, site_dump_folder, search_keywords_config)
                result.analyses.dpo = res
            elif 'find-dpo' in tasks:
                logger.info("Skipping task 'find-dpo': already completed.")
        
        return result

    except Exception as e:
        logger.error(f"FATAL Error processing {result.website_url} ('{result.scenario}'): {e}", exc_info=True)
        result.error = f"{type(e).__name__}: {e}"
        return result
    finally:
        if page and not page.is_closed():
            await page.close()


async def run_sequential_analyses(sites_df: pd.DataFrame, analyzer: PrivacyAnalyzer, browser, args: argparse.Namespace, search_keywords_config: Dict[str, Any], browser_context_config: Dict[str, Any]):
    """
    Orchestrates site analysis sequentially, loading existing results and running only pending tasks.
    """
    for index, row in sites_df.iterrows():
        site_url = row['website_url']
        if not urlparse(site_url).scheme:
            site_url = "https://" + site_url
        
        site_output_dir = get_site_output_dir(site_url)
        site_dump_folder = os.path.join(site_output_dir, "dumps")
        os.makedirs(site_dump_folder, exist_ok=True)
        
        # Load all existing results for this site
        existing_results_map = {res.scenario: res for res in load_site_results(site_url)}
        
        # --- Detection Phase ---
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

        # --- Execution Phase ---
        results_for_this_site = []
        for scenario in scenarios_to_run:
            # Get existing result or create a new one
            result_obj = existing_results_map.get(scenario, SiteAnalysisResult(website_url=site_url, scenario=scenario))
            
            async with await browser.new_context(**browser_context_config) as analysis_context:
                # process_site_scenario modifies the result_obj in place
                processed_result = await process_site_scenario(
                    analysis_context, analyzer, result_obj, site_dump_folder, 
                    search_keywords_config, args.tasks
                )
                results_for_this_site.append(processed_result)
        
        # Save all results for the site after all its scenarios are processed
        save_site_results(results_for_this_site)
    
    logger.info("All sites have been processed.")


async def gdpr_analysis(sites_df: pd.DataFrame, args: argparse.Namespace):
    """
    Orchestrates the setup, execution, and saving of the analysis.
    """
    llm_config = load_llm_config()
    search_keywords_config = load_user_defined_keywords()
    browser_context_config = load_browser_context_config()
    
    llm_provider = OllamaProvider(model=llm_config.get('model', 'llama3'))
    analyzer = PrivacyAnalyzer(llm_client=llm_provider)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        
        await run_sequential_analyses(sites_df, analyzer, browser, args, search_keywords_config, browser_context_config)
        
        await browser.close()


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GDPR-compliant cookie and privacy policy analyzer.")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", type=str, help="A single URL to analyze.")
    group.add_argument("--file", type=str, nargs='?', default="sites.csv", help="Path to a CSV file of URLs. Defaults to 'sites.csv'.")

    tasks = ["all", "cookies", "find-pp", "analyze-pp", "find-cd", "find-dpo", "find-delete", "find-retention"]
    parser.add_argument(
        "--tasks", 
        nargs='+', 
        default=["all"], 
        choices=tasks, 
        help=(
            "Specify which tasks to run. 'all' runs every task. 'cookies' handles cookies. "
            "'find-pp' finds the privacy policy. 'analyze-pp' runs all sub-analyses on the privacy policy "
            "(find-cd, find-dpo, etc.)."
        )
    )
    
    args = parser.parse_args()

    # --- Post-process tasks ---
    # Use a set for efficiency and to avoid duplicates
    requested_tasks = set(args.tasks)
    
    # Define task groups
    all_tasks = {'cookies', 'find-pp', 'find-cd', 'find-dpo', 'find-delete', 'find-retention'}
    analyze_pp_tasks = {'find-cd', 'find-dpo', 'find-delete', 'find-retention'}

    if "all" in requested_tasks:
        final_tasks = all_tasks
    else:
        final_tasks = set()
        if "cookies" in requested_tasks:
            final_tasks.add("cookies")
        if "find-pp" in requested_tasks:
            final_tasks.add("find-pp")
        
        # If analyze-pp is requested, add all its sub-tasks
        if "analyze-pp" in requested_tasks:
            final_tasks.update(analyze_pp_tasks)
        else: # Otherwise, add specific sub-tasks if they were requested
            final_tasks.update(requested_tasks.intersection(analyze_pp_tasks))
            
    # The 'find-pp' task is a dependency for all 'analyze-pp' tasks
    if final_tasks.intersection(analyze_pp_tasks) and 'find-pp' not in final_tasks:
        logger.info("Adding required dependency task: 'find-pp'")
        final_tasks.add('find-pp')
        
    args.tasks = sorted(list(final_tasks))
    return args


def main():
    setup_logging()
    create_output_directories()
    args = parse_args()
    
    logger.info("Starting GDPR Analysis...")
    logger.info(f"Tasks to run: {', '.join(args.tasks)}")

    if args.url:
        logger.info(f"Processing single URL from command line: {args.url}")
        sites_df = pd.DataFrame([{'website_url': args.url}])
    else:
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