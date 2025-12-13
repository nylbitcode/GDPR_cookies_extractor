from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, TypedDict
from datetime import datetime

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
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    error: Optional[str] = None

    # Cookie Info ('scrape' task)
    cookies: List[PlaywrightCookie] = field(default_factory=list)
    simplified_cookies: List[Dict[str, Any]] = field(default_factory=list)
    cookie_categories: List[CookieCategory] = field(default_factory=list)
    third_party_cookie_count: int = 0
    
    # Privacy Policy Info ('find-pp' task)
    privacy_policy_url: Optional[str] = None
    
    # LLM analysis of the privacy policy and other collected links
    llm_privacy_policy_analysis: Dict[str, Any] = field(default_factory=dict)
    simple_extractor_links: Dict[str, List[ExtractedLink]] = field(default_factory=dict)

    # Sub-analyses of the privacy policy ('analyze-pp' tasks)
    analyses: Analyses = field(default_factory=Analyses)
    
    @property
    def cookies_count(self) -> int:
        return len(self.cookies)

    def update_llm_output(self, llm_output: Dict[str, Any]):
        """Safely updates the llm_privacy_policy_analysis dictionary."""
        if llm_output:
            self.llm_privacy_policy_analysis.update(llm_output)

    @classmethod
    def from_exception(cls, site_url: str, scenario: str, e: Exception) -> "SiteAnalysisResult":
        """Creates a result object from an exception."""
        return cls(
            website_url=site_url,
            scenario=scenario,
            error=f"{type(e).__name__}: {e}"
        )