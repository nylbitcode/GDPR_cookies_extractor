from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, TypedDict

class PlaywrightCookie(TypedDict):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: str

@dataclass
class CategorizedCookie:
    name: str
    domain: str
    description: str

@dataclass
class CookieCategory:
    category_name: str
    cookies: List[CategorizedCookie]

@dataclass
class ExtractedLink:
    href: str
    text: str

# --- Sub-Analysis Result Models

@dataclass
class BaseAnalysis:
    reasoning: Optional[str] = None

@dataclass
class CookieDeclarationAnalysis(BaseAnalysis):
    cookie_declaration_url: Optional[str] = None

@dataclass
class DataRetentionAnalysis(BaseAnalysis):
    data_retention_url: Optional[str] = None
    retention_period_summary: Optional[str] = None

@dataclass
class DataDeletionAnalysis(BaseAnalysis):
    data_deletion_url: Optional[str] = None
    deletion_method_summary: Optional[str] = None

@dataclass
class DPOAnalysis(BaseAnalysis):
    dpo_url: Optional[str] = None
    dpo_contact_summary: Optional[str] = None

@dataclass
class Analyses:
    cookie_declaration: Optional[CookieDeclarationAnalysis] = None
    data_retention: Optional[DataRetentionAnalysis] = None
    data_deletion: Optional[DataDeletionAnalysis] = None
    dpo: Optional[DPOAnalysis] = None


# --- Main Result Model 

@dataclass
class SiteAnalysisResult:
    # Core Info
    website_url: str
    scenario: str
    
    # High-level results
    privacy_policy_url: Optional[str] = None
    llm_reasoning: Optional[str] = None 
    
    # Cookie Info
    cookies_count: int = 0
    third_party_cookies_count: int = 0
    raw_cookies_data: List[PlaywrightCookie] = field(default_factory=list)
    categorized_cookies: List[CookieCategory] = field(default_factory=list)
    
    # Extensible dictionary for all sub-analyses
    analyses: Analyses = field(default_factory=Analyses)
    
    # Other collected data
    simple_extractor_links: Dict[str, List[ExtractedLink]] = field(default_factory=dict)

    @staticmethod
    def from_outputs(
        site_url: str,
        scenario: str,
        cookies: List[PlaywrightCookie],
        cookie_categories: List[CookieCategory],
        third_party_count: int,
        llm_output: dict,
        privacy_policy_url: Optional[str] = None,
        simple_extractor_links: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        cookie_declaration: Optional[Dict[str, Any]] = None,
        data_retention: Optional[Dict[str, Any]] = None,
        data_deletion: Optional[Dict[str, Any]] = None,
        dpo: Optional[Dict[str, Any]] = None
    ) -> "SiteAnalysisResult":
        
        
        analyses_container = Analyses(
            cookie_declaration=CookieDeclarationAnalysis(**cookie_declaration) if cookie_declaration else None,
            data_retention=DataRetentionAnalysis(**data_retention) if data_retention else None,
            data_deletion=DataDeletionAnalysis(**data_deletion) if data_deletion else None,
            dpo=DPOAnalysis(**dpo) if dpo else None
        )

        return SiteAnalysisResult(
            website_url=site_url,
            scenario=scenario,
            privacy_policy_url=privacy_policy_url,
            llm_reasoning=llm_output.get("reasoning"),
            cookies_count=len(cookies),
            third_party_cookies_count=third_party_count,
            raw_cookies_data=cookies,
            categorized_cookies=cookie_categories,
            simple_extractor_links=simple_extractor_links if simple_extractor_links is not None else {},
            analyses=analyses_container
        )

    @staticmethod
    def from_exception(
        site_url: str,
        scenario: str,
        e: Exception
    ) -> "SiteAnalysisResult":
        return SiteAnalysisResult(
            website_url=site_url,
            scenario=scenario,
            llm_reasoning=f"Failed to process: {e}",
            analyses=Analyses() 
        )
