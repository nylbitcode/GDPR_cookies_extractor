import json
import logging
import re
import os
from urllib.parse import urljoin, urlparse
from typing import Dict, Any, List, Optional, Tuple
from .llm_interface import AbstractLLMClient
from .models import ExtractedLink, CookieCategory, CategorizedCookie
from dataclasses import asdict
from .scraper import _dump_snapshot
logger = logging.getLogger(__name__)

class PrivacyAnalyzer:
    """
    Analyzes privacy policies and cookie data using a provided LLM client.
    Can operate in a no-LLM mode where it only performs heuristic-based analysis.
    """
    
    def __init__(self, llm_client: Optional[AbstractLLMClient] = None, no_llm: bool = False):
        self.llm_client = llm_client
        self.no_llm = no_llm
        
        if self.no_llm:
            logger.info("PrivacyAnalyzer initialized in no-LLM mode.")
            if self.llm_client:
                logger.warning("An LLM client was provided, but no-LLM mode is active. The client will not be used.")
        elif not self.llm_client:
            raise ValueError("LLM-based analysis requires an llm_client, but none was provided.")
        else:
            logger.info(f"PrivacyAnalyzer initialized with client: {type(llm_client).__name__}")

    # Privacy Policy Methods
    async def _extract_policy_url_from_html(self, html_content: str, url: str, promising_links: List[str]):
        """
        Sends HTML content to the LLM to find the privacy policy URL on a single page.
        """
        prompt = f"""
        You are an expert web analysis agent. Your task is to find the URL of the privacy policy page of this site {url}.
        
        A pre-filtered list of candidate links has been provided: {promising_links}, so choose from these links the most valuable candidate for privacy page. 

        **CRITICAL RULE: If the candidate link list is not empty, you choose the best and most relevant option from that list. Only if the candidates list is empty you can search in the HTML content.** 
        
        When searching, look for links containing keywords like 'privacy policy', 'GDPR', 'data protection', 'privacy center'.
        The privacy policy is often in the footer of the page. Note that the cookie policy and the privacy policy could be on different URLs, so be sure to return the main privacy policy.
        Notice that cookie page and privage page could be on separate pages so do not return return the cookie page in palce of privacy page. 

        The HTML content to analyze is below:
        ---
        {html_content}
        ---
        
        The URL of the page is: {url}

        You MUST return a single JSON object and nothing else. Do not include any text or explanation before or after the JSON object.
        Return your answer as a single JSON object with the following structure:
        {{
          "privacy_policy_url": <string>,
          "reasoning": <string>,
          "confidence_score": <number>
        }}
        - privacy_policy_url: Must be the complete and absolute URL to the privacy page. If no URL is found, this MUST be null.
        - reasoning: Explain your choice or why you could not find a URL.
        - confidence_score: A number from 0.0 to 1.0 indicating your certainty.
        """
        
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "privacy_policy_url": None,
                "reasoning": response.error,
                "confidence_score": 0.0
            }
        
        return response.data

    async def _analyze_page_for_policy(self, page, url: str, site_dump_folder:str, hop_num: int, original_root_domain: str, user_keywords: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        [WORKER FUNCTION]
        Analyzes a page for a policy link, validates the LLM's choice, and calculates a keyword bonus.
        This is the atomic work unit for policy search.
        """
        html_lower = ""
        link_extraction_phases = []
        phase_name = f"find_privacy_policy_hop_{hop_num}"
        try:
            logger.info(f"Analyzing page (Hop {hop_num}): {url}")
            if not page.url == url:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)  # wait for dynamic content to load

            # Get all internal links and dump snapshot
            all_links_objects = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
            
            # Filter for promising links based on keywords
            promising_links_objects = self._filter_promising_links(all_links_objects, user_keywords)

            link_extraction_phases.append({
                "main_link": url,
                "phase": phase_name,
                "promising_extracted_links": [link.href for link in promising_links_objects]
            })

            # Check for external redirect after navigation
            final_netloc = urlparse(page.url).netloc
            if not (final_netloc == original_root_domain or final_netloc.endswith("." + original_root_domain)):
                logger.warning(f"Redirected to external domain: {page.url}. Skipping analysis.")
                return {"privacy_policy_url": None, "reasoning": f"Redirected to external domain {page.url}", "confidence_score": 0.0, "keyword_bonus": 0.0}, link_extraction_phases

            html = await page.content()
            html_lower = html.lower()

            # Call LLM with a simple list of hrefs for the prompt
            href_list_for_llm = [link.href for link in promising_links_objects]
            policy_output = await self._extract_policy_url_from_html(html, url, href_list_for_llm)
            llm_url = policy_output.get("privacy_policy_url")
            logger.debug(f"Returned choice from LLM: {llm_url}")

            # Validate the LLM's choice and apply heuristic override if needed
            if promising_links_objects and llm_url:
                # Check if the LLM's choice is valid -> it's one of the promising hrefs
                is_llm_choice_valid = any(llm_url in link_obj.href for link_obj in promising_links_objects)
                
                if not is_llm_choice_valid:
                    logger.warning(f"LLM disobeyed prompt. Its choice '{llm_url}' was not in the candidate list. Applying heuristic fallback.")
                    
                    # Use the programmatic selector with the rich link objects
                    heuristic_url = self._get_best_candidate(promising_links_objects, user_keywords)
                    
                    if heuristic_url:
                        logger.info(f"Heuristic override selected: '{heuristic_url}'")
                        policy_output["privacy_policy_url"] = heuristic_url
                        policy_output["reasoning"] = "LLM choice overridden by heuristic due to non-compliance. Selected best candidate from pre-filtered list."
                        policy_output["confidence_score"] = 0.95 # High confidence in heuristic -> lower this value when the model confidence increase
                    else:
                        logger.warning("Heuristic fallback found no suitable link either.")
            
            # Calculate keyword bonus
            keyword_bonus = 0.0
            if user_keywords and any(keyword.lower() in html_lower for keyword in user_keywords):
                logger.info(f"User keywords found on {url}, applying bonus.")
                keyword_bonus = 0.3
            
            policy_output['keyword_bonus'] = keyword_bonus

            # absolute url
            found_url = policy_output.get("privacy_policy_url")
            if found_url:
                policy_output["privacy_policy_url"] = urljoin(url, found_url)

            return policy_output, link_extraction_phases
        
        except Exception as e:
            logger.error(f"Error analyzing page {url}: {e}")
            return {"privacy_policy_url": None, "reasoning": f"Failed to analyze page {url}: {e}", "confidence_score": 0.0, "keyword_bonus": 0.0}, []
        finally:
            if page:
                await page.close()

    async def _find_privacy_policy_no_llm_worker(self, page, site_dump_folder: str, filter_keywords: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        [WORKER FUNCTION]
        Analyzes a page for a policy link using only heuristics.
        """
        try:
            logger.info(f"Analyzing page {page.url} for privacy policy in no-LLM mode...")
            await page.wait_for_timeout(3000)  # Wait for dynamic content

            all_links = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, "find_privacy_policy_no_llm", all_links)
            
            promising_links = self._filter_promising_links(all_links, filter_keywords)
            best_url = self._get_best_candidate(promising_links, filter_keywords)
            
            return {"privacy_policy_url": best_url, "reasoning": "Heuristic-based selection"}, []
        except Exception as e:
            logger.error(f"Error during no-LLM privacy policy search: {e}")
            return {"privacy_policy_url": None, "reasoning": str(e)}, []

    async def find_privacy_policy(self, page, site_dump_folder: str, filter_keywords: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        [ORCHESTRATOR FUNCTION]
        Orchestrates the search for the privacy policy URL.
        """
        if self.no_llm:
            return await self._find_privacy_policy_no_llm_worker(page, site_dump_folder, filter_keywords)

        # LLM-based approach
        found_policies = []
        initial_result = None
        link_extraction_phases = []
        site_url = page.url
        
        try:
            logger.info(f"Starting privacy policy search for {site_url}...")
            
            # Determine the root domain to check against redirects
            base_netloc = urlparse(site_url).netloc
            root_domain = base_netloc[4:] if base_netloc.startswith("www.") else base_netloc
            
            # The page is already created and passed in, we just use it for analysis
            initial_result, initial_links = await self._analyze_page_for_policy(
                page, site_url, site_dump_folder, 0, root_domain, filter_keywords
            )
            link_extraction_phases.extend(initial_links)
            
            if initial_result and initial_result.get("privacy_policy_url"):
                found_policies.append(initial_result)

            # FINAL SELECTION
            if found_policies:
                def calculate_hybrid_score(policy):
                    confidence = policy.get('confidence_score', 0.0)
                    bonus = policy.get('keyword_bonus', 0.0)
                    return (0.7 * confidence) + (0.3 * bonus)

                best_policy = max(found_policies, key=calculate_hybrid_score)
                hybrid_score = calculate_hybrid_score(best_policy)
                
                logger.info(f"Selected best privacy policy with hybrid score {hybrid_score:.2f}: {best_policy.get('privacy_policy_url')}")
                return best_policy, link_extraction_phases

            logger.info("No privacy policy found after deep search.")
            return initial_result, link_extraction_phases
        
        except Exception as e:
            logger.error(f"Critical error during privacy policy search for {site_url}: {e}")
            return {"reasoning": f"Failed during privacy policy search: {e}", "privacy_policy_url": None}, []

    async def _search_cookie_declaration(self, page_content: str) -> Dict[str, Any]:
        """
        Asks the LLM to determine if the page content contains a cookie declaration.
        """
        prompt = f"""
        You are an expert in GDPR and web compliance. Your task is to analyze the following text from a web page and determine if it contains a detailed "Cookie Declaration" or "Cookie Policy".

        A "Cookie Declaration" is NOT just a brief mention of cookies. It is a specific section that details the types of cookies used, their purpose, and often includes a list or table of the cookies.

        Look for headings and sections such as:
        - "Cookies Policy"
        - "What are cookies"
        - "Why do we use cookies"
        - "Where do we use cookies?"
        - A table or detailed list of cookies.
        - A categorization of cookies in categories like "Analytical", "Functional" and "Marketing. 

        Analyze the text below:
        ---
        {page_content}
        ---

        Based on your analysis, you MUST return a single JSON object with the following structure:
        {{
          "has_cookie_declaration": <boolean>,
          "reasoning": <string>
        }}
        - has_cookie_declaration: Set to true if you find a detailed cookie declaration or policy section, false otherwise.
        - reasoning: Briefly explain your decision. For example, "The text contains a dedicated 'Cookie Policy' section with a list of cookies." or "The text only mentions cookies briefly without providing details."
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "has_cookie_declaration": False,
                "reasoning": f"LLM query failed: {response.error}"
            }
        
        return response.data

    async def _extract_cookie_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        """
        Sends HTML content and a list of candidate links to the LLM to find the best link to a separate cookie policy page.
        """
        prompt = f"""
        You are an expert web analysis agent. Your task is to find a URL pointing to a "Cookie Policy" or "Cookie Declaration" page from the HTML content of the page: {url}.

        A pre-filtered list of candidate links has been provided: {promising_links}.
        **CRITICAL RULE: If the candidate link list is not empty, you MUST choose the best and most relevant option from that list. Only if the candidates list is empty you can search in the full HTML content.** 

        The privacy policy and cookie policy are often separate. I am on the privacy page, and I need to find the link to the specific cookie policy page.
        Look for anchor tags `<a>` with text like "Cookie Policy", "Statement on Cookies", "Cookie Declaration", or similar phrases.

        The HTML content to analyze is below:
        ---
        {html_content}
        ---
        
        The URL of the current page is: {url}

        You MUST return a single JSON object and nothing else.
        Return your answer as a single JSON object with the following structure:
        {{
          "cookie_policy_link": <string | null>,
          "reasoning": <string>,
          "confidence_score": <number>
        }}
        - cookie_policy_link: Must be the absolute or relative URL to the cookie page. If no link is found, this MUST be null.
        - reasoning: Explain your choice.
        - confidence_score: A number from 0.0 to 1.0 indicating your certainty.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "cookie_policy_link": None,
                "reasoning": response.error,
                "confidence_score": 0.0
            }
        
        return response.data

    async def find_cookie_declaration_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Finds the cookie declaration page using a multi-stage hybrid analysis.
        - Check if the declaration is on the initial privacy policy page.
        - Always try to find a link to a separate, dedicated page.
        - Validate the content of the separate page with another LLM call.
        - Prefer the dedicated page if found and validated, otherwise fall back to the initial page.
        """
        if self.no_llm:
            return {"status": "skipped", "reason": "No-LLM mode"}, []
        if not privacy_policy_url:
            return {"cookie_declaration_url": None, "reasoning": "No privacy policy URL provided."}, []

        page = None
        validation_page = None
        stage1_result = None
        link_extraction_phases = []
        phase_name = "find_cookie_declaration_page_stage_2"
        try:
            # Analyze the initial privacy policy page for content
            logger.info(f"Stage 1: Analyzing for cookie declaration ON the page: {privacy_policy_url}")
            page = await context.new_page()
            await page.goto(privacy_policy_url, timeout=60000, wait_until="domcontentloaded")

            # Snapshot 
            all_links_objects = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
            cookie_keywords = search_keywords_config.get('cookie_declaration', [])
            promising_links_objects = self._filter_promising_links(all_links_objects, cookie_keywords)

            link_extraction_phases.append({
                "main_link": privacy_policy_url,
                "phase": phase_name,
                "all_extracted_links": all_links_objects,
                "promising_extracted_links": [link.href for link in promising_links_objects]
            })

            page_content = await page.evaluate("document.body.innerText")
            if not page_content:
                logger.warning(f"Initial page {privacy_policy_url} has no text content.")
            else:
                llm_content_result = await self._search_cookie_declaration(page_content)
                if llm_content_result.get("has_cookie_declaration"):
                    logger.info(f"Stage 1 SUCCESS: Found cookie declaration directly on {privacy_policy_url}. Storing result and continuing search.")
                    stage1_result = {
                        "cookie_declaration_url": privacy_policy_url,
                        "reasoning": llm_content_result.get('reasoning')
                    }

            logger.info("Stage 2: Starting HYBRID search for a separate cookie policy link.")

            # Hybrid model to find the best candidate link
            if not promising_links_objects:
                logger.info("No promising links found for a separate page.")
                if stage1_result:
                    logger.info("No separate link found, returning Stage 1 result.")
                    return stage1_result, link_extraction_phases
                return {"cookie_declaration_url": None, "reasoning": "Declaration not on page, and no links with relevant keywords found."}, link_extraction_phases

            html_content = await page.content()
            href_list_for_llm = [link.href for link in promising_links_objects]
            llm_link_choice_result = await self._extract_cookie_link_from_html(html_content, privacy_policy_url, href_list_for_llm)
            llm_chosen_link = llm_link_choice_result.get("cookie_policy_link")
            
            final_candidate_url = None
            is_llm_choice_valid = any(llm_chosen_link in link_obj.href for link_obj in promising_links_objects) if llm_chosen_link else False

            if llm_chosen_link and is_llm_choice_valid:
                final_candidate_url = llm_chosen_link
            else:
                logger.warning("LLM choice was invalid or missing. Applying heuristic fallback.")
                heuristic_choice = self._get_best_candidate(promising_links_objects, cookie_keywords)
                if heuristic_choice:
                    final_candidate_url = heuristic_choice
                else:
                    logger.error("Heuristic fallback also failed to select a candidate.")
                    if stage1_result:
                        logger.info("Link search failed, returning Stage 1 result.")
                        return stage1_result, link_extraction_phases
                    return {"cookie_declaration_url": None, "reasoning": "LLM and heuristic both failed to choose a link."}, link_extraction_phases

            full_candidate_url = urljoin(privacy_policy_url, final_candidate_url)
            logger.info(f"Hybrid model selected link: {full_candidate_url}. Stage 3: Validating content.")

            # Validate the content of the final candidate page
            validation_page = await context.new_page()
            await validation_page.goto(full_candidate_url, timeout=60000, wait_until="domcontentloaded")
            
            validation_content = await validation_page.evaluate("document.body.innerText")
            if not validation_content:
                logger.warning(f"Candidate page {full_candidate_url} has no text content to validate.")
                if stage1_result:
                    return stage1_result, link_extraction_phases
                return {"cookie_declaration_url": None, "reasoning": f"Found link {full_candidate_url}, but the page was empty."}, link_extraction_phases

            validation_llm_result = await self._search_cookie_declaration(validation_content)

            if validation_llm_result.get("has_cookie_declaration"):
                logger.info(f"SUCCESS: Confirmed that {full_candidate_url} contains the cookie declaration. This is the preferred result.")
                return {
                    "cookie_declaration_url": full_candidate_url,
                    "reasoning": f"Found and validated separate cookie policy at {full_candidate_url}."
                }, link_extraction_phases
            else:
                logger.info(f"Validation of separate page {full_candidate_url} failed. Reason: {validation_llm_result.get('reasoning')}")
                if stage1_result:
                    logger.info("Falling back to Stage 1 result.")
                    return stage1_result, link_extraction_phases
                return {"cookie_declaration_url": None, "reasoning": f"Found link {full_candidate_url}, but content validation failed and no initial declaration was found."}, link_extraction_phases

        except Exception as e:
            logger.error(f"Error during multi-stage cookie declaration search for {privacy_policy_url}: {e}")
            if stage1_result:
                return stage1_result, link_extraction_phases
            return {"cookie_declaration_url": None, "reasoning": f"An exception occurred: {e}"}, link_extraction_phases
        finally:
            if page:
                await page.close()
            if validation_page:
                await validation_page.close()

    async def _search_data_retention_declaration(self, page_content: str) -> Dict[str, Any]:
        """
        Asks the LLM to determine if the page content contains a data retention declaration
        and to extract a summary of the retention period.
        """
        prompt = f"""
        You are an expert in GDPR and web compliance. Your task is to analyze the following text from a web page to determine if it contains a "Data Retention" policy and to summarize the retention period if present.

        - **Analyze for Policy:** First, determine if the text contains a specific section about data retention. This is NOT just a brief mention. It should detail how long data is kept. Look for headings like "Data Retention", "How long we keep your data", or "Retention of Personal Information".
        - **Extract Retention Period:** If a data retention section is found, carefully read it and extract a concise summary of the data retention periods. For example: "User data is kept for the duration of the account plus 30 days", "Analytics data is retained for 26 months", or "Data is kept as long as necessary for legal and business purposes."

        **CRITICAL RULE:** Do NOT invent information. If the text does not explicitly state a retention period or the policy is vague (e.g., "we keep data for as long as needed"), you MUST set the summary to null.

        Analyze the text below:
        ---
        {page_content}
        ---

        Based on your analysis, you MUST return a single JSON object with the following structure:
        {{
          "has_data_retention_declaration": <boolean>,
          "reasoning": <string>,
          "retention_period_summary": <string | null>
        }}
        - has_data_retention_declaration: Set to true if you find a detailed data retention policy section, false otherwise.
        - reasoning: Briefly explain your decision.
        - retention_period_summary: A concise summary of the retention period if found. If no specific period is mentioned, this MUST be null.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "has_data_retention_declaration": False,
                "reasoning": f"LLM query failed: {response.error}",
                "retention_period_summary": None
            }
        
        return response.data

    async def _extract_data_retention_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        """
        Sends HTML content and a list of candidate links to the LLM to find the best link to a separate data retention policy page.
        """
        prompt = f"""
        You are an expert web analysis agent. Your task is to find a URL pointing to a "Data Retention Policy" or "Data Storage Information" page from the HTML content of the page: {url}.

        A pre-filtered list of candidate links has been provided: {promising_links}.
        **CRITICAL RULE: If the candidate link list is not empty, you MUST choose the best and most relevant option from that list. Only if the candidates list is empty you can search in the full HTML content.** 

        The privacy policy and data retention policy might be separate. I am on the privacy page, and I need to find the link to the specific data retention policy page.
        Look for anchor tags `<a>` with text like "Data Retention", "Storage Periods", "How long we store your data", or similar phrases.

        The HTML content to analyze is below:
        ---
        {html_content}
        ---
        
        The URL of the current page is: {url}

        You MUST return a single JSON object and nothing else.
        Return your answer as a single JSON object with the following structure:
        {{
          "data_retention_policy_link": <string | null>,
          "reasoning": <string>,
          "confidence_score": <number>
        }}
        - data_retention_policy_link: Must be the absolute or relative URL to the data retention page. If no link is found, this MUST be null.
        - reasoning: Explain your choice.
        - confidence_score: A number from 0.0 to 1.0 indicating your certainty.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "data_retention_policy_link": None,
                "reasoning": response.error,
                "confidence_score": 0.0
            }
        
        return response.data

    async def find_data_retention_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Finds the data retention page using a multi-stage hybrid analysis, mirroring the cookie declaration search logic.
        - Check if the declaration is on the initial privacy policy page.
        - Always try to find a link to a separate, dedicated page.
        - Validate the content of the separate page.
        - Prefer the dedicated page if found and validated, otherwise fall back to the initial page.
        """
        if self.no_llm:
            return {"status": "skipped", "reason": "No-LLM mode"}, []
        if not privacy_policy_url:
            return {"data_retention_url": None, "reasoning": "No privacy policy URL provided."}, []

        page = None
        validation_page = None
        stage1_result = None
        link_extraction_phases = []
        phase_name = "find_data_retention_page_stage_2"
        try:
            # Analyze the initial privacy policy page for content
            logger.info(f"Stage 1: Analyzing for data retention ON the page: {privacy_policy_url}")
            page = await context.new_page()
            await page.goto(privacy_policy_url, timeout=60000, wait_until="domcontentloaded")
            
            # Snapshot
            all_links_objects = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
            data_retention_keywords = search_keywords_config.get('data_retention', [])
            promising_links_objects = self._filter_promising_links(all_links_objects, data_retention_keywords)

            link_extraction_phases.append({
                "main_link": privacy_policy_url,
                "phase": phase_name,
                "all_extracted_links": all_links_objects,
                "promising_extracted_links": [link.href for link in promising_links_objects]
            })
            
            page_content = await page.evaluate("document.body.innerText")
            if not page_content:
                logger.warning(f"Initial page {privacy_policy_url} has no text content.")
            else:
                llm_content_result = await self._search_data_retention_declaration(page_content)
                if llm_content_result.get("has_data_retention_declaration"):
                    logger.info(f"Stage 1 SUCCESS: Found data retention policy directly on {privacy_policy_url}. Storing result.")
                    stage1_result = {
                        "data_retention_url": privacy_policy_url,
                        "reasoning": llm_content_result.get('reasoning'),
                        "retention_period_summary": llm_content_result.get('retention_period_summary')
                    }
            
            logger.info("Stage 2: Starting HYBRID search for a separate data retention link.")

            # Hybrid model to find the best candidate link
            if not promising_links_objects:
                logger.info("No promising links found for a separate data retention page.")
                if stage1_result:
                    logger.info("No separate link found, returning Stage 1 result.")
                    return stage1_result, link_extraction_phases
                return {"data_retention_url": None, "reasoning": "Policy not on page, and no links with relevant keywords found."}, link_extraction_phases

            html_content = await page.content()
            href_list_for_llm = [link.href for link in promising_links_objects]
            llm_link_choice_result = await self._extract_data_retention_link_from_html(html_content, privacy_policy_url, href_list_for_llm)
            llm_chosen_link = llm_link_choice_result.get("data_retention_policy_link")

            final_candidate_url = None
            is_llm_choice_valid = any(llm_chosen_link in link_obj.href for link_obj in promising_links_objects) if llm_chosen_link else False

            if llm_chosen_link and is_llm_choice_valid:
                final_candidate_url = llm_chosen_link
            else:
                logger.warning("LLM choice for data retention was invalid or missing. Applying heuristic fallback.")
                heuristic_choice = self._get_best_candidate(promising_links_objects, data_retention_keywords)
                if heuristic_choice:
                    final_candidate_url = heuristic_choice
                else:
                    logger.error("Heuristic fallback for data retention also failed.")
                    if stage1_result:
                        return stage1_result, link_extraction_phases
                    return {"data_retention_url": None, "reasoning": "LLM and heuristic both failed to choose a link."}, link_extraction_phases

            full_candidate_url = urljoin(privacy_policy_url, final_candidate_url)
            logger.info(f"Hybrid model selected data retention link: {full_candidate_url}. Stage 3: Validating content.")

            # Validate the content of the final candidate page
            validation_page = await context.new_page()
            await validation_page.goto(full_candidate_url, timeout=60000, wait_until="domcontentloaded")
            
            validation_content = await validation_page.evaluate("document.body.innerText")
            if not validation_content:
                logger.warning(f"Candidate data retention page {full_candidate_url} has no text content.")
                if stage1_result:
                    return stage1_result, link_extraction_phases
                return {"data_retention_url": None, "reasoning": f"Found link {full_candidate_url}, but the page was empty."}, link_extraction_phases

            validation_llm_result = await self._search_data_retention_declaration(validation_content)

            if validation_llm_result.get("has_data_retention_declaration"):
                logger.info(f"SUCCESS: Confirmed that {full_candidate_url} contains the data retention policy. This is the preferred result.")
                return {
                    "data_retention_url": full_candidate_url,
                    "reasoning": f"Found and validated separate data retention policy at {full_candidate_url}.",
                    "retention_period_summary": validation_llm_result.get('retention_period_summary')
                }, link_extraction_phases
            else:
                logger.info(f"Validation of separate data retention page {full_candidate_url} failed. Reason: {validation_llm_result.get('reasoning')}")
                if stage1_result:
                    logger.info("Falling back to Stage 1 result for data retention.")
                    return stage1_result, link_extraction_phases
                return {"data_retention_url": None, "reasoning": f"Found link {full_candidate_url}, but content validation failed and no initial policy was found."}, link_extraction_phases

        except Exception as e:
            logger.error(f"Error during data retention page search for {privacy_policy_url}: {e}")
            if stage1_result:
                return stage1_result, link_extraction_phases
            return {"data_retention_url": None, "reasoning": f"An exception occurred: {e}"}, link_extraction_phases
        finally:
            if page:
                await page.close()
            if validation_page:
                await validation_page.close()

    async def _search_data_deletion_declaration(self, page_content: str) -> Dict[str, Any]:
        """
        Asks the LLM to determine if the page content contains a data deletion declaration
        and to extract a summary of how to delete data.
        """
        prompt = f"""
        You are an expert in GDPR and web compliance. Your task is to analyze the following text from a web page to determine if it contains a "Data Deletion" policy and to summarize how a user can delete their data.

        - **Analyze for Policy:** First, determine if the text contains a specific section about data deletion or user rights to erasure. Look for headings like "Data Deletion", "Your Right to Erasure", "Deleting Your Information", or "Managing Your Data".
        - **Extract Deletion Method:** If a data deletion section is found, carefully read it and extract a concise summary of the method for deleting data. For example: "Users can delete their data from their account settings dashboard", "A data deletion request can be sent to privacy@example.com", or "Data is deleted automatically upon account closure."

        **CRITICAL RULE:** Do NOT invent information. If the text does not explicitly state how to delete data, you MUST set the summary to null.

        Analyze the text below:
        ---
        {page_content}
        ---

        Based on your analysis, you MUST return a single JSON object with the following structure:
        {{
          "has_data_deletion_declaration": <boolean>,
          "reasoning": <string>,
          "deletion_method_summary": <string | null>
        }}
        - has_data_deletion_declaration: Set to true if you find a detailed data deletion policy section, false otherwise.
        - reasoning: Briefly explain your decision.
        - deletion_method_summary: A concise summary of how a user can delete their data. If no specific method is mentioned, this MUST be null.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "has_data_deletion_declaration": False,
                "reasoning": f"LLM query failed: {response.error}",
                "deletion_method_summary": None
            }
        
        return response.data

    async def _extract_data_deletion_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        """
        Sends HTML content and a list of candidate links to the LLM to find the best link to a separate data deletion page.
        """
        prompt = f"""
        You are an expert web analysis agent. Your task is to find a URL pointing to a "Data Deletion", "Privacy Dashboard", or "Manage Your Data" page from the HTML content of the page: {url}.

        A pre-filtered list of candidate links has been provided: {promising_links}.
        **CRITICAL RULE: If the candidate link list is not empty, you MUST choose the best and most relevant option from that list. Only if the candidates list is empty you can search in the full HTML content.** 

        The privacy policy and data deletion instructions might be on separate pages. I am on the privacy page, and I need to find the link to a specific page for managing or deleting data.
        Look for anchor tags `<a>` with text like "Delete Your Data", "Data Deletion", "Privacy Dashboard", "Manage Your Information", or similar phrases.

        The HTML content to analyze is below:
        ---
        {html_content}
        ---
        
        The URL of the current page is: {url}

        You MUST return a single JSON object and nothing else.
        Return your answer as a single JSON object with the following structure:
        {{
          "data_deletion_policy_link": <string | null>,
          "reasoning": <string>,
          "confidence_score": <number>
        }}
        - data_deletion_policy_link: Must be the absolute or relative URL to the data deletion page. If no link is found, this MUST be null.
        - reasoning: Explain your choice.
        - confidence_score: A number from 0.0 to 1.0 indicating your certainty.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "data_deletion_policy_link": None,
                "reasoning": response.error,
                "confidence_score": 0.0
            }
        
        return response.data

    async def find_data_deletion_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Finds the data deletion page using a multi-stage hybrid analysis.
        """
        if self.no_llm:
            return {"status": "skipped", "reason": "No-LLM mode"}, []
        if not privacy_policy_url:
            return {"data_deletion_url": None, "reasoning": "No privacy policy URL provided."}, []

        page = None
        validation_page = None
        stage1_result = None
        link_extraction_phases = []
        phase_name = "find_data_deletion_page_stage_2"
        try:
            # Analyze the initial privacy policy page for content
            logger.info(f"Stage 1: Analyzing for data deletion ON the page: {privacy_policy_url}")
            page = await context.new_page()
            await page.goto(privacy_policy_url, timeout=60000, wait_until="domcontentloaded")
            
            # --- Snapshot and Link Extraction
            all_links_objects = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
            data_deletion_keywords = search_keywords_config.get('data_deletion', [])
            promising_links_objects = self._filter_promising_links(all_links_objects, data_deletion_keywords)
            
            link_extraction_phases.append({
                "main_link": privacy_policy_url,
                "phase": phase_name,
                "all_extracted_links": all_links_objects,
                "promising_extracted_links": [link.href for link in promising_links_objects]
            })

            page_content = await page.evaluate("document.body.innerText")
            if not page_content:
                logger.warning(f"Initial page {privacy_policy_url} has no text content.")
            else:
                llm_content_result = await self._search_data_deletion_declaration(page_content)
                if llm_content_result.get("has_data_deletion_declaration"):
                    logger.info(f"Stage 1 SUCCESS: Found data deletion info directly on {privacy_policy_url}. Storing result.")
                    stage1_result = {
                        "data_deletion_url": privacy_policy_url,
                        "reasoning": llm_content_result.get('reasoning'),
                        "deletion_method_summary": llm_content_result.get('deletion_method_summary')
                    }
            
            logger.info("Stage 2: Starting HYBRID search for a separate data deletion link.")

            # Hybrid model to find the best candidate link
            if not promising_links_objects:
                logger.info("No promising links found for a separate data deletion page.")
                if stage1_result:
                    logger.info("No separate link found, returning Stage 1 result.")
                    return stage1_result, link_extraction_phases
                return {"data_deletion_url": None, "reasoning": "Policy not on page, and no links with relevant keywords found."}, link_extraction_phases

            html_content = await page.content()
            href_list_for_llm = [link.href for link in promising_links_objects]
            llm_link_choice_result = await self._extract_data_deletion_link_from_html(html_content, privacy_policy_url, href_list_for_llm)
            llm_chosen_link = llm_link_choice_result.get("data_deletion_policy_link")

            final_candidate_url = None
            is_llm_choice_valid = any(llm_chosen_link in link_obj.href for link_obj in promising_links_objects) if llm_chosen_link else False

            if llm_chosen_link and is_llm_choice_valid:
                final_candidate_url = llm_chosen_link
            else:
                logger.warning("LLM choice for data deletion was invalid or missing. Applying heuristic fallback.")
                heuristic_choice = self._get_best_candidate(promising_links_objects, data_deletion_keywords)
                if heuristic_choice:
                    final_candidate_url = heuristic_choice
                else:
                    logger.error("Heuristic fallback for data deletion also failed.")
                    if stage1_result:
                        return stage1_result, link_extraction_phases
                    return {"data_deletion_url": None, "reasoning": "LLM and heuristic both failed to choose a link."}, link_extraction_phases

            full_candidate_url = urljoin(privacy_policy_url, final_candidate_url)
            logger.info(f"Hybrid model selected data deletion link: {full_candidate_url}. Stage 3: Validating content.")

            # Stage 3: Validate the content of the final candidate page
            validation_page = await context.new_page()
            await validation_page.goto(full_candidate_url, timeout=60000, wait_until="domcontentloaded")
            
            validation_content = await validation_page.evaluate("document.body.innerText")
            if not validation_content:
                logger.warning(f"Candidate data deletion page {full_candidate_url} has no text content.")
                if stage1_result:
                    return stage1_result, link_extraction_phases
                return {"data_deletion_url": None, "reasoning": f"Found link {full_candidate_url}, but the page was empty."}, link_extraction_phases

            validation_llm_result = await self._search_data_deletion_declaration(validation_content)

            if validation_llm_result.get("has_data_deletion_declaration"):
                logger.info(f"SUCCESS: Confirmed that {full_candidate_url} contains the data deletion policy. This is the preferred result.")
                return {
                    "data_deletion_url": full_candidate_url,
                    "reasoning": f"Found and validated separate data deletion policy at {full_candidate_url}.",
                    "deletion_method_summary": validation_llm_result.get('deletion_method_summary')
                }, link_extraction_phases
            else:
                logger.info(f"Validation of separate data deletion page {full_candidate_url} failed. Reason: {validation_llm_result.get('reasoning')}")
                if stage1_result:
                    logger.info("Falling back to Stage 1 result for data deletion.")
                    return stage1_result, link_extraction_phases
                return {"data_deletion_url": None, "reasoning": f"Found link {full_candidate_url}, but content validation failed and no initial policy was found."}, link_extraction_phases

        except Exception as e:
            logger.error(f"Error during data deletion page search for {privacy_policy_url}: {e}")
            if stage1_result:
                return stage1_result, link_extraction_phases
            return {"data_deletion_url": None, "reasoning": f"An exception occurred: {e}"}, link_extraction_phases
        finally:
            if page:
                await page.close()
            if validation_page:
                await validation_page.close()

    async def _ask_llm_about_dpo_declaration(self, page_content: str) -> Dict[str, Any]:
        """
        Asks the LLM to determine if the page content contains DPO information
        and to extract contact details.
        """
        prompt = f"""
        You are an expert in GDPR and web compliance. Your task is to analyze the following text to determine if it contains contact information for a Data Protection Officer (DPO) or a privacy representative.

        - **Analyze for DPO Section:** Look for headings like "Data Protection Officer", "DPO", "Privacy Contact", "Data Controller", or "Contact Us for Privacy Matters".
        - **Extract Contact Details:** If a relevant section is found, extract a concise summary of the contact methods. This can include:
            - Email addresses (e.g., dpo@example.com, privacy@example.com)
            - Physical mailing addresses.
            - Links to contact forms.
            - Phone numbers.

        **CRITICAL RULE:** Do NOT invent information. If the text does not explicitly state contact details for a DPO or privacy representative, you MUST set the summary to null.

        Analyze the text below:
        ---
        {page_content}
        ---

        Based on your analysis, you MUST return a single JSON object with the following structure:
        {{
          "has_dpo_declaration": <boolean>,
          "reasoning": <string>,
          "dpo_contact_summary": <string | null>
        }}
        - has_dpo_declaration: Set to true if you find a DPO or privacy contact section.
        - reasoning: Briefly explain your decision.
        - dpo_contact_summary: A concise summary of the contact details (email, address, form link). If no specific details are found, this MUST be null.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "has_dpo_declaration": False,
                "reasoning": f"LLM query failed: {response.error}",
                "dpo_contact_summary": None
            }
        
        return response.data

    async def _extract_dpo_link_from_html(self, html_content: str, url: str, promising_links: List[str]) -> Dict[str, Any]:
        """
        Sends HTML content and a list of candidate links to the LLM to find the best link to a separate DPO/contact page.
        """
        prompt = f"""
        You are an expert web analysis agent. Your task is to find a URL pointing to a "Data Protection Officer (DPO)", "Privacy Contact", or "Data Controller" page from the HTML content of the page: {url}.

        A pre-filtered list of candidate links has been provided: {promising_links}.
        **CRITICAL RULE: If the candidate link list is not empty, you MUST choose the best and most relevant option from that list. Only if the candidates list is empty you can search in the full HTML content.** 

        Look for anchor tags `<a>` with text like "DPO", "Data Protection Officer", "Contact our DPO", "Privacy Contact", or similar phrases.

        The HTML content to analyze is below:
        ---
        {html_content}
        ---
        
        The URL of the current page is: {url}

        You MUST return a single JSON object and nothing else.
        Return your answer as a single JSON object with the following structure:
        {{
          "dpo_policy_link": <string | null>,
          "reasoning": <string>,
          "confidence_score": <number>
        }}
        - dpo_policy_link: Must be the absolute or relative URL to the DPO contact page. If no link is found, this MUST be null.
        - reasoning: Explain your choice.
        - confidence_score: A number from 0.0 to 1.0 indicating your certainty.
        """
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success:
            return {
                "dpo_policy_link": None,
                "reasoning": response.error,
                "confidence_score": 0.0
            }
        
        return response.data

    async def find_dpo_page(self, context, privacy_policy_url: str, site_dump_folder: str, search_keywords_config: Dict[str, List[str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Finds the DPO contact page using a multi-stage hybrid analysis.
        """
        if self.no_llm:
            return {"status": "skipped", "reason": "No-LLM mode"}, []
        if not privacy_policy_url:
            return {"dpo_url": None, "reasoning": "No privacy policy URL provided."}, []

        page = None
        validation_page = None
        stage1_result = None
        link_extraction_phases = []
        phase_name = "find_dpo_page_stage_2"
        try:
            # Analyze the initial privacy policy page for content
            logger.info(f"Stage 1: Analyzing for DPO information ON the page: {privacy_policy_url}")
            page = await context.new_page()
            await page.goto(privacy_policy_url, timeout=60000, wait_until="domcontentloaded")

            # Snapshot
            all_links_objects = await self._extract_all_internal_links(page)
            await _dump_snapshot(page, site_dump_folder, phase_name, all_links_objects)
            dpo_keywords = search_keywords_config.get('dpo', [])
            promising_links_objects = self._filter_promising_links(all_links_objects, dpo_keywords)
            
            link_extraction_phases.append({
                "main_link": privacy_policy_url,
                "phase": phase_name,
                "all_extracted_links": all_links_objects,
                "promising_extracted_links": [link.href for link in promising_links_objects]
            })

            page_content = await page.evaluate("document.body.innerText")
            if not page_content:
                logger.warning(f"Initial page {privacy_policy_url} has no text content.")
            else:
                llm_content_result = await self._ask_llm_about_dpo_declaration(page_content)
                if llm_content_result.get("has_dpo_declaration"):
                    logger.info(f"Stage 1 SUCCESS: Found DPO info directly on {privacy_policy_url}. Storing result.")
                    stage1_result = {
                        "dpo_url": privacy_policy_url,
                        "reasoning": llm_content_result.get('reasoning'),
                        "dpo_contact_summary": llm_content_result.get('dpo_contact_summary')
                    }
            
            logger.info("Stage 2: Starting HYBRID search for a separate DPO contact link.")

            # Hybrid model to find the best candidate link
            if not promising_links_objects:
                logger.info("No promising links found for a separate DPO page.")
                if stage1_result:
                    logger.info("No separate link found, returning Stage 1 result.")
                    return stage1_result, link_extraction_phases
                return {"dpo_url": None, "reasoning": "DPO info not on page, and no links with relevant keywords found."}, link_extraction_phases

            html_content = await page.content()
            href_list_for_llm = [link.href for link in promising_links_objects]
            llm_link_choice_result = await self._extract_dpo_link_from_html(html_content, privacy_policy_url, href_list_for_llm)
            llm_chosen_link = llm_link_choice_result.get("dpo_policy_link")

            final_candidate_url = None
            is_llm_choice_valid = any(llm_chosen_link in link_obj.href for link_obj in promising_links_objects) if llm_chosen_link else False

            if llm_chosen_link and is_llm_choice_valid:
                final_candidate_url = llm_chosen_link
            else:
                logger.warning("LLM choice for DPO was invalid or missing. Applying heuristic fallback.")
                heuristic_choice = self._get_best_candidate(promising_links_objects, dpo_keywords)
                if heuristic_choice:
                    final_candidate_url = heuristic_choice
                else:
                    logger.error("Heuristic fallback for DPO also failed.")
                    if stage1_result:
                        return stage1_result, link_extraction_phases
                    return {"dpo_url": None, "reasoning": "LLM and heuristic both failed to choose a link."}, link_extraction_phases

            full_candidate_url = urljoin(privacy_policy_url, final_candidate_url)
            logger.info(f"Hybrid model selected DPO link: {full_candidate_url}. Stage 3: Validating content.")

            # Validate the content of the final candidate page
            validation_page = await context.new_page()
            await validation_page.goto(full_candidate_url, timeout=60000, wait_until="domcontentloaded")
            
            validation_content = await validation_page.evaluate("document.body.innerText")
            if not validation_content:
                logger.warning(f"Candidate DPO page {full_candidate_url} has no text content.")
                if stage1_result:
                    return stage1_result, link_extraction_phases
                return {"dpo_url": None, "reasoning": f"Found link {full_candidate_url}, but the page was empty."}, link_extraction_phases

            validation_llm_result = await self._ask_llm_about_dpo_declaration(validation_content)

            if validation_llm_result.get("has_dpo_declaration"):
                logger.info(f"SUCCESS: Confirmed that {full_candidate_url} contains the DPO information. This is the preferred result.")
                return {
                    "dpo_url": full_candidate_url,
                    "reasoning": f"Found and validated separate DPO page at {full_candidate_url}.",
                    "dpo_contact_summary": validation_llm_result.get('dpo_contact_summary')
                }, link_extraction_phases
            else:
                logger.info(f"Validation of separate DPO page {full_candidate_url} failed. Reason: {validation_llm_result.get('reasoning')}")
                if stage1_result:
                    logger.info("Falling back to Stage 1 result for DPO information.")
                    return stage1_result, link_extraction_phases
                return {"dpo_url": None, "reasoning": f"Found link {full_candidate_url}, but content validation failed and no initial DPO info was found."}, link_extraction_phases

        except Exception as e:
            logger.error(f"Error during DPO page search for {privacy_policy_url}: {e}")
            if stage1_result:
                return stage1_result, link_extraction_phases
            return {"dpo_url": None, "reasoning": f"An exception occurred: {e}"}, link_extraction_phases
        finally:
            if page:
                await page.close()
            if validation_page:
                await validation_page.close()

    # Cookie Analysis
    async def categorize_cookies(self, cookies_data: list) -> List[CookieCategory]:
        """
        Categorizes a list of cookies using the LLM.
        """
        if self.no_llm:
            return []
        if not cookies_data:
            return []

        cookies_json_list = json.dumps(cookies_data, indent=2)
        prompt = f"""
        You are an expert in GDPR compliance and a JSON-only generator.
        Your task is to categorize a list of cookies and provide a brief description for each, based on your general knowledge.

        INPUT: A JSON list of raw cookie objects.
        OUTPUT: A single JSON object, with no other text.

        CATEGORIES DEFINITIONS:
        - "Strictly Necessary": Essential for website function (e.g., session, security, shopping cart).
        - "Functional": Remembers user choices (e.g., language, preferences).
        - "Analytical": Collects data on user behavior (e.g., Google Analytics).
        - "Marketing": Tracks users for advertising.
        - "Uncategorized": Unknown or generic purpose.

        INSTRUCTIONS:
        - Analyze each cookie in the "Input Cookies" list.
        - Based on the cookie's "name" and "domain", categorize it into one of the five categories defined above.
        - Create a "description" for each cookie based on your general knowledge (e.g., a cookie named "_ga" is for Google Analytics).
        - CRITICAL RULE: If a cookie's name is generic or unknown (e.g., "uid", "session_token"), you MUST set its description to "No specific description available." Do NOT invent a purpose.
        - Return a single JSON object with the root key "cookie_categories".
        - The value of "cookie_categories" must be a list of objects (one for each category that contains cookies).
        - Each category object must contain:
            - "category_name": The name of the category.
            - "cookies": A list of objects for the cookies in that category.
        - Each cookie object in the *output* "cookies" list MUST have this structure:
            - "name": The original cookie name.
            - "domain": The original cookie domain.
            - "description": Your generated description (or "No specific description available.").

        DO NOT include any text, explanation, or markdown before or after the JSON object.

        EXAMPLE OF REQUIRED OUTPUT FORMAT:
        {{{{
          "cookie_categories": [
            {{
              "category_name": "Strictly Necessary",
              "cookies": [
                {{ "name": "sessionid", "domain": "example.com", "description": "No specific description available." }}
              ]
            }},
            {{
              "category_name": "Analytical",
              "cookies": [
                {{ "name": "_ga", "domain": ".example.com", "description": "Google Analytics cookie used to distinguish users." }}
              ]
            }}
          ]
        }}}}

        INPUT COOKIES TO CATEGORIZE:
        {cookies_json_list}
        """
        
        response = await self.llm_client.query_json(user_prompt=prompt)
        
        if not response.success or "cookie_categories" not in response.data:
            logger.error(f"Cookie categorization failed or returned malformed data: {response.error}")
            return []
            
        # Parse the dictionary into dataclass objects
        categorized_list = []
        for cat_data in response.data.get("cookie_categories", []):
            try:
                cookies_in_cat = [CategorizedCookie(**c) for c in cat_data.get("cookies", [])]
                categorized_list.append(
                    CookieCategory(
                        category_name=cat_data.get("category_name"),
                        cookies=cookies_in_cat
                    )
                )
            except (TypeError, KeyError) as e:
                logger.warning(f"Skipping malformed cookie category object: {cat_data}. Error: {e}")
        
        return categorized_list



    # Utility Functions

    def _filter_promising_links(self, all_links: List[ExtractedLink], filter_keywords: List[str]) -> List[ExtractedLink]:
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

    async def _extract_all_internal_links(self, page) -> List[ExtractedLink]:
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
    
    def _get_best_candidate(self, promising_links: List[ExtractedLink], keyword_priority_list: List[str]) -> Optional[str]:
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