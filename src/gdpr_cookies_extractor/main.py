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
    load_user_defined_keywords,
    load_site_results
)
from .analysis.scraper import handle_cookie_banner
from .analysis.ollama_providers import OllamaProvider
from .analysis.privacy_analyzers import PrivacyAnalyzer
from .analysis.models import SiteAnalysisResult, ExtractedLink


logger = logging.getLogger(__name__)


async def _dump_snapshot(page, site_dump_folder: str, phase: str, all_links: List[ExtractedLink]):
    """Dumps the HTML and all extracted links for a specific analysis phase."""
    try:
        # Ensure the site-specific dump directory exists
        os.makedirs(site_dump_folder, exist_ok=True)
        
        # Dump HTML
        html_content = await page.content()
        html_dump_path = os.path.join(site_dump_folder, f"{phase}.html")
        with open(html_dump_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        # Dump all links
        links_dump_path = os.path.join(site_dump_folder, f"{phase}_links.json")
        with open(links_dump_path, "w", encoding="utf-8") as f:
            json.dump([asdict(link) for link in all_links], f, indent=4, ensure_ascii=False)
        
        logger.info(f"Dumped snapshot for phase '{phase}' to {site_dump_folder}")

    except Exception as e:
        logger.error(f"Failed to dump snapshot for phase '{phase}': {e}")


def _get_best_candidate(promising_links: List[ExtractedLink], keyword_priority_list: List[str]) -> Optional[str]:
    """
    Selects the best URL from a list of candidates using a weighted scoring system
    based on a prioritized list of keywords.
    """
    if not promising_links or not keyword_priority_list:
        return None

    best_link_href = None
    max_score = -1
    num_keywords = len(keyword_priority_list)

    for link_data in promising_links:
        current_score = 0
        # Iterate through keywords to calculate a score for the current link
        for i, keyword in enumerate(keyword_priority_list):
            # Higher priority keywords (earlier in the list) get a higher base weight
            weight = num_keywords - i
            
            # Split keyword phrase into individual words
            required_words = keyword.lower().split()

            # Give a higher score for matches in the anchor text (strong signal)
            if all(word in link_data.text.lower() for word in required_words):
                current_score += weight * 2
            
            # Give a lower score for matches in the URL itself
            if all(word in link_data.href.lower() for word in required_words):
                current_score += weight

        # Update the best link if the current one has a better score
        if current_score > max_score:
            max_score = current_score
            best_link_href = link_data.href
        # Tie-breaker: if scores are equal, prefer the shorter link
        elif current_score == max_score and best_link_href:
            if len(link_data.href) < len(best_link_href):
                best_link_href = link_data.href
    
    if best_link_href:
        logger.info(f"Heuristic selection: chose '{best_link_href}' with score {max_score}")
    else:
        logger.info("Heuristic selection: no suitable candidate found.")

    return best_link_href


def _filter_promising_links(all_links: List[ExtractedLink], filter_keywords: List[str]) -> List[ExtractedLink]:
    """
    Filters a list of link objects based on a list of keywords.
    """
    if not filter_keywords:
        return []

    promising_links = []
    lower_keywords = [k.lower() for k in filter_keywords]

    for link in all_links:
        search_area = link.href.lower() + " " + link.text.lower()
        if any(keyword in search_area for keyword in lower_keywords):
            promising_links.append(link)
    
    return promising_links


async def _extract_all_internal_links(page) -> List[ExtractedLink]:
    """
    Helper to extract all internal links from a page,
    Returning both the URL and the anchor text.
    """
    links = []
    unique_hrefs = set()
    site_url = page.url

    base_netloc = urlparse(site_url).netloc
    root_domain = base_netloc[4:] if base_netloc.startswith("www.") else base_netloc 
    
    for a in await page.query_selector_all('a'):
        href = None
        try:
            href = await a.get_attribute('href')
            if href:
                full_url = urljoin(site_url, href)
                
                if full_url in unique_hrefs:
                    continue

                link_netloc = urlparse(full_url).netloc 
                
                is_exact_domain = (link_netloc == root_domain)
                is_subdomain = link_netloc.endswith("." + root_domain)
                
                if (is_exact_domain or is_subdomain) and '#' not in full_url and not full_url.endswith(('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.xml', '.json', '.zip', '.rar', '.tar', '.gz', '.svg', '.ico')):
                    text_content = (await a.inner_text() or "").strip()
                    links.append(ExtractedLink(href=full_url, text=text_content))
                    unique_hrefs.add(full_url)
        except Exception as e:
            logger.debug(f"Could not process link {href}: {e}")

    logger.info(f"Found {len(links)} total internal links on {page.url}")
    for link in links:
        logger.debug(f"  - Found internal link: {link.href} (Text: '{link.text}')")
    return links


async def find_privacy_policy_no_llm(page, site_dump_folder: str, filter_keywords: List[str]) -> Optional[str]:
    """
    Finds the privacy policy URL without using an LLM.
    """
    phase_name = "find_privacy_policy_no_llm"
    try:
        logger.info("Analyzing page for privacy policy without LLM...")
        await page.wait_for_timeout(3000)  # wait for dynamic content to load

        # Get all internal links and dump snapshot
        all_links_objects = await _extract_all_internal_links(page)
        await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
        
        # Filter for promising links based on keywords
        promising_links_objects = _filter_promising_links(all_links_objects, filter_keywords)

        # Get the best candidate using the heuristic
        best_candidate_url = _get_best_candidate(promising_links_objects, filter_keywords)

        return best_candidate_url

    except Exception as e:
        logger.error(f"Error finding privacy policy without LLM: {e}")
        return None


async def process_site_scenario_no_llm(
    context, 
    result: SiteAnalysisResult,
    site_dump_folder: str, 
    search_keywords_config: Dict[str, List[str]],
    tasks: List[str]
) -> SiteAnalysisResult:
    """
    Runs the selected analysis tasks for a single site and a single cookie scenario without LLM.
    This function modifies the 'result' object in place.
    """
    page = None
    try:
        set_log_context(result.website_url, result.scenario)
        logger.info(f"Processing tasks {tasks} for {result.website_url} (Scenario: {result.scenario}) with no-llm")

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
                # Skip cookie categorization
                result.third_party_cookie_count = count_third_party_cookies(result.website_url, cookies)
        elif 'cookies' in tasks:
            logger.info("Skipping task 'cookies': already completed.")

        # --- Task: Find Privacy Policy ('find-pp') ---
        if 'find-pp' in tasks and not result.privacy_policy_url:
            logger.info("Running task: 'find-pp' (no-llm)")
            # Ensure we are on the main page before searching for the privacy policy
            if not page.url or urlparse(page.url).netloc != urlparse(result.website_url).netloc:
                 await page.goto(result.website_url, wait_until="domcontentloaded", timeout=60000)

            privacy_policy_url = await find_privacy_policy_no_llm(
                page, site_dump_folder,
                filter_keywords=search_keywords_config.get('privacy_policy', []),
            )
            if privacy_policy_url:
                result.privacy_policy_url = urljoin(page.url, privacy_policy_url)
                logger.info(f"Found privacy policy url: {result.privacy_policy_url}")
        elif 'find-pp' in tasks:
            logger.info("Skipping task 'find-pp': already completed.")
        
        return result

    except Exception as e:
        logger.error(f"FATAL Error processing {result.website_url} ('{result.scenario}') with no-llm: {e}", exc_info=True)
        result.error = f"{type(e).__name__}: {e}"
        return result
    finally:
        if page and not page.is_closed():
            await page.close()


async def run_sequential_analyses_no_llm(sites_df: pd.DataFrame, browser, args: argparse.Namespace, search_keywords_config: Dict[str, Any], browser_context_config: Dict[str, Any]):
    """
    Orchestrates site analysis sequentially for the no-llm case.
    """
    for index, row in sites_df.iterrows():
        site_url = row['website_url']
        if not urlparse(site_url).scheme:
            site_url = "https://" + site_url
        
        site_output_dir = get_site_output_dir(site_url)
        site_dump_folder = os.path.join(site_output_dir, "dumps")
        os.makedirs(site_dump_folder, exist_ok=True)
        
        existing_results_map = {res.scenario: res for res in load_site_results(site_url)}
        
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

        results_for_this_site = []
        for scenario in scenarios_to_run:
            result_obj = existing_results_map.get(scenario, SiteAnalysisResult(website_url=site_url, scenario=scenario))
            
            if args.privacy_policy_url and not result_obj.privacy_policy_url:
                logger.info(f"Using provided privacy policy URL: {args.privacy_policy_url}")
                result_obj.privacy_policy_url = args.privacy_policy_url
                
            async with await browser.new_context(**browser_context_config) as analysis_context:
                processed_result = await process_site_scenario_no_llm(
                    analysis_context, result_obj, site_dump_folder, 
                    search_keywords_config, args.tasks
                )
                results_for_this_site.append(processed_result)
        
        save_site_results(results_for_this_site)
    
    logger.info("All sites have been processed.")


async def gdpr_analysis_no_llm(sites_df: pd.DataFrame, args: argparse.Namespace):
    """
    Orchestrates the setup, execution, and saving of the analysis without LLM.
    """
    search_keywords_config = load_user_defined_keywords()
    browser_context_config = load_browser_context_config()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        
        await run_sequential_analyses_no_llm(sites_df, browser, args, search_keywords_config, browser_context_config)
        
        await browser.close()


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
            
            # If a privacy policy URL is provided via CLI, inject it into the result object
            if args.privacy_policy_url and not result_obj.privacy_policy_url:
                logger.info(f"Using provided privacy policy URL: {args.privacy_policy_url}")
                result_obj.privacy_policy_url = args.privacy_policy_url
                
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
    
    parser.add_argument("--no-llm", action="store_true", help="Disable all LLM-based analysis.")

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
    
    parser.add_argument("--privacy-policy-url", type=str, help="URL of the privacy policy to analyze. Skips the 'find-pp' task.")
    
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
    
    # If --no-llm is selected, remove all tasks that depend on the LLM
    if args.no_llm:
        llm_dependent_tasks = analyze_pp_tasks | {'analyze-pp'}
        removed_tasks = final_tasks.intersection(llm_dependent_tasks)
        if removed_tasks:
            logger.info(f"(--no-llm) Removing LLM-dependent tasks: {', '.join(sorted(list(removed_tasks)))}")
        final_tasks -= llm_dependent_tasks
        
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
            
    if args.no_llm:
        logger.info("Running in --no-llm mode. LLM-based analysis will be skipped.")
        asyncio.run(gdpr_analysis_no_llm(sites_df, args))
    else:
        asyncio.run(gdpr_analysis(sites_df, args))


if __name__ == "__main__":
    main()