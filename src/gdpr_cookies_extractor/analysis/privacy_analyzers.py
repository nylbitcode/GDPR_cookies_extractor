import json
import logging
import re
import os
from urllib.parse import urljoin, urlparse
from typing import Dict, Any, List, Optional, Tuple, Callable, Coroutine
import asyncio

from .llm_interface import AbstractLLMClient
from .models import LinkExtractionPhase

logger = logging.getLogger(__name__)

class PrivacyAnalyzer:
    """
    Analyzes privacy policies and cookie data using a provided LLM client.
    This class contains methods to find various policy pages (Privacy, Cookie, etc.)
    by orchestrating a breadth-first search across web pages.
    """
    
    def __init__(self, llm_client: AbstractLLMClient, timestamp: str):
        self.llm_client = llm_client
        self.timestamp = timestamp
        logger.info(f"PrivacyAnalyzer initialized with client: {type(llm_client).__name__}")

    async def _dump_snapshot(self, page, site_dump_folder: str, phase: str, all_links: List[Dict]):
        """Dumps the HTML and all extracted links for a specific analysis phase."""
        try:
            os.makedirs(site_dump_folder, exist_ok=True)
            html_content = await page.content()
            html_dump_path = os.path.join(site_dump_folder, f"{phase}.html")
            with open(html_dump_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            links_dump_path = os.path.join(site_dump_folder, f"{phase}_links.json")
            with open(links_dump_path, "w", encoding="utf-8") as f:
                json.dump(all_links, f, indent=4, ensure_ascii=False)
            
            logger.info(f"Dumped snapshot for phase '{phase}' to {site_dump_folder}")
        except Exception as e:
            logger.error(f"Failed to dump snapshot for phase '{phase}': {e}")

    async def _analyze_page_for_specific_policy(
        self,
        context,
        url: str,
        site_dump_folder: str,
        hop_num: int,
        original_root_domain: str,
        phase_name_prefix: str,
        keywords: List[str],
        content_validator: Callable[[str], Coroutine[Any, Any, Dict[str, Any]]],
        link_extractor: Callable[[str, str, List[str]], Coroutine[Any, Any, Dict[str, Any]]],
        link_key: str
    ) -> Tuple[Optional[Dict[str, Any]], List[LinkExtractionPhase], List[Dict[str, str]]]:
        """
        [GENERIC WORKER] Analyzes a single page for a specific policy.
        - Validates content using `content_validator`.
        - Extracts links using `link_extractor`.
        - Dumps snapshots.
        """
        page = None
        phase_name = f"{phase_name_prefix}_hop_{hop_num}"
        try:
            logger.info(f"Analyzing page (Hop {hop_num}): {url}")
            page = await context.new_page()
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")

            # --- Snapshot and Link Extraction ---
            all_links = await self._extract_all_internal_links(page)
            await self._dump_snapshot(page, site_dump_folder, phase_name, all_links)
            promising_links = self._filter_promising_links(all_links, keywords)
            
            link_extraction_phase = LinkExtractionPhase(
                main_link=url,
                phase=phase_name,
                all_extracted_links=all_links,
                promising_extracted_links=[link['href'] for link in promising_links]
            )

            # --- Content and Link Analysis ---
            found_policy_info = None
            page_content = await page.evaluate("document.body.innerText")
            
            if page_content:
                validation_result = await content_validator(page_content)
                if validation_result.get(f"has_{link_key}_declaration"):
                    logger.info(f"Content validation successful on {url} for {link_key}.")
                    found_policy_info = validation_result
                    found_policy_info[f"{link_key}_url"] = url  # The policy is on this page
            
            # Use LLM to find a link to a dedicated page, even if content is found
            html_content = await page.content()
            href_list_for_llm = [link['href'] for link in promising_links]
            link_extraction_result = await link_extractor(html_content, url, href_list_for_llm)
            
            return found_policy_info, [link_extraction_phase], promising_links

        except Exception as e:
            logger.error(f"Error analyzing page {url} for {link_key}: {e}", exc_info=True)
            return None, [], []
        finally:
            if page:
                await page.close()

    async def _search_for_policy_page(
        self,
        context,
        initial_url: str,
        site_dump_folder: str,
        search_keywords: List[str],
        content_validator: Callable,
        link_extractor: Callable,
        link_key: str,
        phase_name_prefix: str,
        max_hops: int,
        fan_out: int
    ) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        """
        [GENERIC ORCHESTRATOR] Performs a breadth-first search for a specific policy page.
        """
        logger.info(f"Starting search for '{link_key}' from {initial_url} (max_hops={max_hops}, fan_out={fan_out})")
        
        visited_urls = set()
        urls_to_process = {initial_url}
        all_found_policies = []
        all_link_extraction_phases = []
        
        original_root_domain = urlparse(initial_url).netloc.replace("www.", "")

        for hop_num in range(max_hops + 1):
            if not urls_to_process:
                logger.info(f"Hop {hop_num}: No new URLs to process. Ending search for {link_key}.")
                break

            logger.info(f"Hop {hop_num}: Processing {len(urls_to_process)} URLs for {link_key}.")
            
            tasks = []
            for url in urls_to_process:
                if url not in visited_urls:
                    visited_urls.add(url)
                    tasks.append(self._analyze_page_for_specific_policy(
                        context, url, site_dump_folder, hop_num, original_root_domain, 
                        phase_name_prefix, search_keywords, content_validator, link_extractor, link_key
                    ))

            results_this_hop = await asyncio.gather(*tasks)
            
            next_urls_to_process = set()
            all_promising_links_this_hop = []

            for policy_info, extraction_phases, promising_links in results_this_hop:
                all_link_extraction_phases.extend(extraction_phases)
                if policy_info:
                    all_found_policies.append(policy_info)
                all_promising_links_this_hop.extend(promising_links)

            if hop_num < max_hops:
                best_candidates_for_next_hop = self._get_best_candidates(
                    all_promising_links_this_hop, search_keywords, fan_out
                )
                for candidate_url in best_candidates_for_next_hop:
                    if candidate_url not in visited_urls:
                        next_urls_to_process.add(candidate_url)
            
            urls_to_process = next_urls_to_process
        
        if all_found_policies:
            # Simple strategy: prefer the first one found. Can be enhanced later.
            best_policy = all_found_policies[0]
            logger.info(f"Found {len(all_found_policies)} potential policies for {link_key}. Selecting first one: {best_policy.get(f'{link_key}_url')}")
            return best_policy, all_link_extraction_phases

        logger.info(f"No '{link_key}' policy found after deep search.")
        return {f"{link_key}_url": None, "reasoning": f"No {link_key} page found."}, all_link_extraction_phases

    # --- Public-Facing Find Methods (Wrappers) ---

    async def find_privacy_policy(self, context, site_url: str, site_dump_folder: str, filter_keywords: Optional[List[str]] = None, max_hops: int = 1, fan_out: int = 1) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        # This function is a special case as it doesn't have a content validator, it only extracts links.
        # So we adapt it slightly from the generic search pattern.
        return await self._search_for_policy_page(
            context,
            initial_url=site_url,
            site_dump_folder=site_dump_folder,
            search_keywords=filter_keywords or [],
            content_validator=lambda x: asyncio.sleep(0, result={}), # No-op content validator
            link_extractor=self._extract_policy_url_from_html,
            link_key="privacy_policy",
            phase_name_prefix="find_privacy_policy",
            max_hops=max_hops,
            fan_out=fan_out
        )

    async def find_cookie_declaration_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 1, fan_out: int = 1) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        return await self._search_for_policy_page(
            context,
            initial_url=privacy_policy_url,
            site_dump_folder=site_dump_folder,
            search_keywords=search_keywords_config.get('cookie_declaration', []),
            content_validator=self._ask_llm_about_cookie_declaration,
            link_extractor=self._extract_cookie_link_from_html,
            link_key="cookie_declaration",
            phase_name_prefix="find_cookie_declaration",
            max_hops=max_hops,
            fan_out=fan_out
        )

    async def find_data_retention_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 1, fan_out: int = 1) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        return await self._search_for_policy_page(
            context,
            initial_url=privacy_policy_url,
            site_dump_folder=site_dump_folder,
            search_keywords=search_keywords_config.get('data_retention', []),
            content_validator=self._ask_llm_about_data_retention_declaration,
            link_extractor=self._extract_data_retention_link_from_html,
            link_key="data_retention",
            phase_name_prefix="find_data_retention",
            max_hops=max_hops,
            fan_out=fan_out
        )

    async def find_data_deletion_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 1, fan_out: int = 1) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        return await self._search_for_policy_page(
            context,
            initial_url=privacy_policy_url,
            site_dump_folder=site_dump_folder,
            search_keywords=search_keywords_config.get('data_deletion', []),
            content_validator=self._ask_llm_about_data_deletion_declaration,
            link_extractor=self._extract_data_deletion_link_from_html,
            link_key="data_deletion",
            phase_name_prefix="find_data_deletion",
            max_hops=max_hops,
            fan_out=fan_out
        )

    async def find_dpo_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 1, fan_out: int = 1) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        return await self._search_for_policy_page(
            context,
            initial_url=privacy_policy_url,
            site_dump_folder=site_dump_folder,
            search_keywords=search_keywords_config.get('dpo', []),
            content_validator=self._ask_llm_about_dpo_declaration,
            link_extractor=self._extract_dpo_link_from_html,
            link_key="dpo",
            phase_name_prefix="find_dpo",
            max_hops=max_hops,
            fan_out=fan_out
        )

    # --- LLM Functions ---

    async def _extract_policy_url_from_html(self, html_content: str, url: str, promising_links: List[str]):
        # This is now primarily a link extractor, not a content validator.
        # The generic orchestrator handles content validation separately.
        prompt = f"""You are an expert web analysis agent. From the page {url}, find the URL of the main privacy policy page. A pre-filtered list of candidate links is: {promising_links}. Choose the best option from that list. If the list is empty, you may search the full HTML. Return a single JSON object with the key "privacy_policy_url" containing the absolute URL, or null if not found."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"privacy_policy_url": None}

    async def _ask_llm_about_cookie_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text to determine if it contains a detailed "Cookie Declaration" or "Cookie Policy", not just a brief mention. A detailed declaration often lists cookie types, purposes, and includes a table. Return a JSON with "has_cookie_declaration": <boolean> and "reasoning": <string>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"has_cookie_declaration": False, "reasoning": f"LLM query failed: {response.error}"}

    async def _extract_cookie_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL pointing to a "Cookie Policy" or "Cookie Declaration" page. Candidates: {promising_links}. Choose the best from the list, or search the HTML if empty. Return JSON with "cookie_declaration_url": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"cookie_declaration_url": None}

    async def _ask_llm_about_data_retention_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text to see if it contains a detailed "Data Retention" policy. If so, summarize the retention period. Return JSON with "has_data_retention_declaration": <boolean>, "reasoning": <string>, and "retention_period_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"has_data_retention_declaration": False, "reasoning": f"LLM query failed: {response.error}", "retention_period_summary": None}

    async def _extract_data_retention_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL pointing to a "Data Retention" page. Candidates: {promising_links}. Choose the best. Return JSON with "data_retention_url": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"data_retention_url": None}
        
    async def _ask_llm_about_data_deletion_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text for a "Data Deletion" policy. If found, summarize the deletion method. Return JSON with "has_data_deletion_declaration": <boolean>, "reasoning": <string>, and "deletion_method_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"has_data_deletion_declaration": False, "reasoning": f"LLM query failed: {response.error}", "deletion_method_summary": None}

    async def _extract_data_deletion_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL for "Data Deletion" or "Privacy Dashboard". Candidates: {promising_links}. Choose the best. Return JSON with "data_deletion_url": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"data_deletion_url": None}

    async def _ask_llm_about_dpo_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text for a "Data Protection Officer (DPO)" contact section. If found, summarize the contact details. Return JSON with "has_dpo_declaration": <boolean>, "reasoning": <string>, and "dpo_contact_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"has_dpo_declaration": False, "reasoning": f"LLM query failed: {response.error}", "dpo_contact_summary": None}

    async def _extract_dpo_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL for a "DPO" or "Privacy Contact" page. Candidates: {promising_links}. Choose the best. Return JSON with "dpo_url": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else {"dpo_url": None}

    # --- Cookie Analysis Methods ---
    async def categorize_cookies(self, cookies_data: list):
        # ... (This function remains unchanged)
        return {}
    
    # --- UTILITY FUNCTIONS ---
    
    def _filter_promising_links(self, all_links: List[Dict[str, str]], filter_keywords: List[str]) -> List[Dict[str, str]]:
        if not filter_keywords: return []
        promising_links = []
        lower_keywords = [k.lower() for k in filter_keywords]
        for link in all_links:
            search_area = link["href"].lower() + " " + link["text"].lower()
            if any(keyword in search_area for keyword in lower_keywords):
                promising_links.append(link)
        return promising_links

    async def _extract_all_internal_links(self, page) -> List[Dict[str, str]]:
        links, unique_hrefs = [], set()
        base_netloc = urlparse(page.url).netloc.replace("www.", "")
        for a in await page.query_selector_all('a'):
            try:
                href = await a.get_attribute('href')
                if not href: continue
                full_url, link_netloc = urljoin(page.url, href), urlparse(urljoin(page.url, href)).netloc
                if full_url in unique_hrefs: continue
                if (link_netloc == base_netloc or link_netloc.endswith("." + base_netloc)) and '#' not in full_url:
                    if not any(full_url.endswith(ext) for ext in ['.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.xml', '.json', '.zip', '.rar', '.svg', '.ico']):
                        links.append({"href": full_url, "text": (await a.inner_text() or "").strip()})
                        unique_hrefs.add(full_url)
            except Exception as e:
                logger.debug(f"Could not process link {href}: {e}")
        logger.debug(f"Found {len(links)} total internal links on {page.url}")
        return links
    
    def _get_best_candidates(self, promising_links: List[Dict[str, str]], keyword_priority_list: List[str], fan_out: int) -> List[str]:
        if not promising_links or not keyword_priority_list: return []
        scored_links = []
        num_keywords = len(keyword_priority_list)
        for link_data in promising_links:
            current_score = 0
            for i, keyword in enumerate(keyword_priority_list):
                weight = num_keywords - i
                required_words = keyword.lower().split()
                if all(word in link_data["text"].lower() for word in required_words): current_score += weight * 2
                if all(word in link_data["href"].lower() for word in required_words): current_score += weight
            if current_score > 0: scored_links.append({"href": link_data["href"], "score": current_score})
        
        scored_links.sort(key=lambda x: (-x["score"], len(x["href"])))
        
        top_urls, seen_urls = [], set()
        for link in scored_links:
            if len(top_urls) >= fan_out: break
            if link["href"] not in seen_urls:
                top_urls.append(link["href"])
                seen_urls.add(link["href"])
        
        logger.info(f"Heuristic selection: chose {len(top_urls)} candidates for fan-out: {top_urls}")
        return top_urls

    def _get_best_candidate(self, promising_links: List[Dict[str, str]], keyword_priority_list: List[str]) -> Optional[str]:
        top_candidates = self._get_best_candidates(promising_links, keyword_priority_list, 1)
        return top_candidates[0] if top_candidates else None
