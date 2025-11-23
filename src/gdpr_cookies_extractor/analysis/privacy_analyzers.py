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
    """
    
    def __init__(self, llm_client: AbstractLLMClient, timestamp: str):
        self.llm_client = llm_client
        self.timestamp = timestamp
        logger.info(f"PrivacyAnalyzer initialized with client: {type(llm_client).__name__}")

    async def _dump_snapshot(self, page, site_dump_folder: str, phase: str, all_links: List[Dict]):
        try:
            os.makedirs(site_dump_folder, exist_ok=True)
            html_content = await page.content()
            with open(os.path.join(site_dump_folder, f"{phase}.html"), "w", encoding="utf-8") as f:
                f.write(html_content)
            with open(os.path.join(site_dump_folder, f"{phase}_links.json"), "w", encoding="utf-8") as f:
                json.dump(all_links, f, indent=4, ensure_ascii=False)
            logger.info(f"Dumped snapshot for phase '{phase}' to {site_dump_folder}")
        except Exception as e:
            logger.error(f"Failed to dump snapshot for phase '{phase}': {e}")

    # --- Generic Search Framework ---

    async def _analyze_page_for_specific_policy(
        self, context, url: str, site_dump_folder: str, hop_num: int,
        phase_name_prefix: str, keywords: List[str], link_key: str,
        content_validator: Optional[Callable] = None, link_extractor: Optional[Callable] = None
    ) -> Tuple[Optional[Dict[str, Any]], LinkExtractionPhase, List[Dict[str, str]]]:
        page = None
        phase_name = f"{phase_name_prefix}_hop_{hop_num}"
        try:
            logger.info(f"Analyzing page (Hop {hop_num}): {url}")
            page = await context.new_page()
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            all_links = await self._extract_all_internal_links(page)
            await self._dump_snapshot(page, site_dump_folder, phase_name, all_links)
            promising_links = self._filter_promising_links(all_links, keywords)
            extraction_phase = LinkExtractionPhase(main_link=url, phase=phase_name, all_extracted_links=all_links, promising_extracted_links=[l['href'] for l in promising_links])
            
            found_policy_info = None
            
            if content_validator:
                page_content = await page.evaluate("document.body.innerText")
                if page_content:
                    validation_result = await content_validator(page_content)
                    if validation_result and validation_result.get(f"has_{link_key}"):
                        logger.info(f"Content validation successful on {url}.")
                        found_policy_info = validation_result
                        found_policy_info['url'] = url
            
            if link_extractor:
                html_content = await page.content()
                href_list_for_llm = [link['href'] for link in promising_links]
                link_output = await link_extractor(html_content, url, href_list_for_llm)
                if link_output and link_output.get(f"{link_key}_url"):
                    if not found_policy_info:
                        found_policy_info = link_output
                        
            return found_policy_info, extraction_phase, promising_links
        except Exception as e:
            logger.error(f"Error in _analyze_page_for_specific_policy for {url}: {e}", exc_info=True)
            return None, LinkExtractionPhase(main_link=url, phase=phase_name), []
        finally:
            if page: await page.close()

    async def _search_for_policy_page(
        self, context, initial_url: str, site_dump_folder: str, search_keywords: List[str],
        phase_name_prefix: str, link_key: str, max_hops: int, fan_out: int,
        content_validator: Optional[Callable] = None, link_extractor: Optional[Callable] = None
    ) -> Tuple[Dict[str, Any], List[LinkExtractionPhase]]:
        
        visited_urls, urls_to_process = set(), {initial_url}
        all_found_policies, all_link_extraction_phases = [], []
        
        for hop_num in range(max_hops + 1):
            if not urls_to_process: break
            logger.info(f"Hop {hop_num}: Processing {len(urls_to_process)} URLs for {link_key}.")
            
            tasks = [self._analyze_page_for_specific_policy(context, url, site_dump_folder, hop_num, phase_name_prefix, search_keywords, link_key, content_validator, link_extractor) for url in urls_to_process if url not in visited_urls]
            visited_urls.update(urls_to_process)
            
            results_this_hop = await asyncio.gather(*tasks)
            
            next_urls_to_process, all_promising_links_this_hop = set(), []
            for policy_info, extraction_phase, promising_links in results_this_hop:
                all_link_extraction_phases.append(extraction_phase)
                if policy_info: all_found_policies.append(policy_info)
                all_promising_links_this_hop.extend(promising_links)
            
            if hop_num < max_hops:
                best_candidates = self._get_best_candidates(all_promising_links_this_hop, search_keywords, fan_out)
                next_urls_to_process = {url for url in best_candidates if url not in visited_urls}
            urls_to_process = next_urls_to_process
            
        if all_found_policies:
            best_policy = max(all_found_policies, key=lambda p: (1 if p.get(f"has_{link_key}") else 0, p.get('confidence_score', 0.0)))
            final_url = best_policy.get('url') or best_policy.get(f"{link_key}_url")
            logger.info(f"Selected best {link_key} page: {final_url}")
            best_policy[f"{link_key}_url"] = final_url
            return best_policy, [p.__dict__ for p in all_link_extraction_phases]

        return {f"{link_key}_url": None, "reasoning": f"No {link_key} page found after deep search."}, [p.__dict__ for p in all_link_extraction_phases]

    # --- Public Find Methods ---

    async def find_privacy_policy(self, context, site_url: str, site_dump_folder: str, filter_keywords: Optional[List[str]] = None, max_hops: int = 0, fan_out: int = 1) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._search_for_policy_page(context, site_url, site_dump_folder, filter_keywords or [], "find_privacy_policy", "privacy_policy", max_hops, fan_out, link_extractor=self._extract_policy_url_from_html)

    async def find_cookie_declaration_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 0, fan_out: int = 1) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._search_for_policy_page(context, privacy_policy_url, site_dump_folder, search_keywords_config.get('cookie_declaration', []), "find_cookie_declaration", "cookie_declaration", max_hops, fan_out, content_validator=self._ask_llm_about_cookie_declaration, link_extractor=self._extract_cookie_link_from_html)

    async def find_data_retention_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 0, fan_out: int = 1) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._search_for_policy_page(context, privacy_policy_url, site_dump_folder, search_keywords_config.get('data_retention', []), "find_data_retention", "data_retention", max_hops, fan_out, content_validator=self._ask_llm_about_data_retention_declaration, link_extractor=self._extract_data_retention_link_from_html)

    async def find_data_deletion_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 0, fan_out: int = 1) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._search_for_policy_page(context, privacy_policy_url, site_dump_folder, search_keywords_config.get('data_deletion', []), "find_data_deletion", "data_deletion", max_hops, fan_out, content_validator=self._ask_llm_about_data_deletion_declaration, link_extractor=self._extract_data_deletion_link_from_html)

    async def find_dpo_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]], max_hops: int = 0, fan_out: int = 1) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._search_for_policy_page(context, privacy_policy_url, site_dump_folder, search_keywords_config.get('dpo', []), "find_dpo", "dpo", max_hops, fan_out, content_validator=self._ask_llm_about_dpo_declaration, link_extractor=self._extract_dpo_link_from_html)

    # --- LLM Helpers (with full original prompts) ---

    async def _extract_policy_url_from_html(self, html_content: str, url: str, promising_links: List[str]):
        prompt = f"""
        You are an expert web analysis agent. Your task is to find the URL of the privacy policy page of this site {url}.
        A pre-filtered list of candidate links has been provided: {promising_links}.
        **CRITICAL RULE: If the candidate link list is not empty, you must choose the best and most relevant option from that list. Only if the candidates list is empty can you search in the HTML content.** 
        When searching, look for links containing keywords like 'privacy policy', 'GDPR', 'data protection', 'privacy center'. The privacy policy is often in the footer of the page.
        You MUST return a single JSON object with the following structure: {{ "privacy_policy_url": <string|null>, "reasoning": <string>, "confidence_score": <number> }}
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _ask_llm_about_cookie_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text to determine if it contains a detailed "Cookie Declaration" or "Cookie Policy", not just a brief mention. A detailed declaration often lists cookie types, purposes, and includes a table. Return a JSON with "has_cookie_declaration": <boolean> and "reasoning": <string>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _extract_cookie_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL pointing to a "Cookie Policy" or "Cookie Declaration" page. Candidates: {promising_links}. Choose the best from the list. Return JSON with "cookie_declaration_url": <string|null>, "reasoning": <string>, "confidence_score": <number>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _ask_llm_about_data_retention_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text for a "Data Retention" policy. If found, extract a summary of the retention period. CRITICAL RULE: Do NOT invent information. If the text does not explicitly state a retention period, set summary to null. Return JSON with "has_data_retention": <boolean>, "reasoning": <string>, "retention_period_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _extract_data_retention_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL for a "Data Retention" page. Candidates: {promising_links}. Return JSON with "data_retention_url": <string|null>, "reasoning": <string>, "confidence_score": <number>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _ask_llm_about_data_deletion_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text for a "Data Deletion" policy. If found, summarize the deletion method. CRITICAL RULE: Do NOT invent information. Return JSON with "has_data_deletion": <boolean>, "reasoning": <string>, "deletion_method_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _extract_data_deletion_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL for a "Data Deletion" or "Privacy Dashboard" page. Candidates: {promising_links}. Return JSON with "data_deletion_url": <string|null>, "reasoning": <string>, "confidence_score": <number>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _ask_llm_about_dpo_declaration(self, page_content: str) -> Dict[str, Any]:
        prompt = f"""Analyze the text for a "Data Protection Officer (DPO)" contact section. If found, summarize contact details. Return JSON with "has_dpo": <boolean>, "reasoning": <string>, "dpo_contact_summary": <string | null>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    async def _extract_dpo_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        prompt = f"""From {url}, find a URL for a "DPO" or "Privacy Contact" page. Candidates: {promising_links}. Return JSON with "dpo_url": <string|null>, "reasoning": <string>, "confidence_score": <number>."""
        response = await self.llm_client.query_json(user_prompt=prompt)
        return response.data if response.success else None

    # --- Other Helpers ---
    async def categorize_cookies(self, cookies_data: list): return {} # Placeholder
    
    def _filter_promising_links(self, all_links: List[Dict[str, str]], keywords: List[str]) -> List[Dict[str, str]]:
        if not keywords: return []
        promising_links = []
        for link in all_links:
            search_area = link["href"].lower() + " " + link["text"].lower()
            if any(keyword.lower() in search_area for keyword in keywords): promising_links.append(link)
        return promising_links

    async def _extract_all_internal_links(self, page) -> List[Dict[str, str]]:
        links, unique_hrefs = [], set()
        base_netloc = urlparse(page.url).netloc.replace("www.", "")
        for a in await page.query_selector_all('a'):
            try:
                href = await a.get_attribute('href')
                if not href: continue
                full_url = urljoin(page.url, href)
                if full_url in unique_hrefs: continue
                link_netloc = urlparse(full_url).netloc
                if (link_netloc == base_netloc or link_netloc.endswith("." + base_netloc)) and '#' not in full_url and not any(full_url.endswith(ext) for ext in ['.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.xml', '.json', '.zip', '.rar', '.svg', '.ico']):
                    links.append({"href": full_url, "text": (await a.inner_text() or "").strip()})
                    unique_hrefs.add(full_url)
            except Exception: pass
        return links

    def _get_best_candidates(self, promising_links: List[Dict[str, str]], keyword_priority_list: List[str], fan_out: int) -> List[str]:
        if not promising_links or not keyword_priority_list: return []
        scored_links, unique_hrefs = [], set()
        for link_data in promising_links:
            if link_data['href'] in unique_hrefs: continue
            current_score = 0
            for keyword in keyword_priority_list:
                if keyword.lower() in link_data["text"].lower() or keyword.lower() in link_data["href"].lower(): current_score += 1
            if current_score > 0:
                scored_links.append({"href": link_data["href"], "score": current_score})
                unique_hrefs.add(link_data['href'])
        scored_links.sort(key=lambda x: (-x["score"], len(x["href"])))
        top_urls = [link["href"] for link in scored_links[:fan_out]]
        logger.info(f"Heuristic selection: chose {len(top_urls)} candidates for fan-out: {top_urls}")
        return top_urls
        
    def _get_best_candidate(self, promising_links: List[Dict[str, str]], keyword_priority_list: List[str]) -> Optional[str]:
        top_candidates = self._get_best_candidates(promising_links, keyword_priority_list, 1)
        return top_candidates[0] if top_candidates else None