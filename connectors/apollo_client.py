import requests
from typing import Optional

from runtime_config import get_required_setting

# See docs/ICP.md for full rationale behind these filters.

# Segment 1: Software/SaaS, 200-1,000 employees
ICP_SEGMENT1_FILTERS = {
    "organization_num_employees_ranges": ["200,1000"],
    "q_organization_keyword_tags": [
        "marketo", "hubspot", "eloqua", "pardot", "salesforce",
        "computer software", "saas", "information technology",
    ],
}

# Segment 2: Non-software verticals, 500-10,000 employees
ICP_SEGMENT2_FILTERS = {
    "organization_num_employees_ranges": ["500,10000"],
    "q_organization_keyword_tags": [
        "marketo", "hubspot", "eloqua", "pardot", "salesforce",
        "financial services", "biotech", "pharma", "healthcare",
        "telecom", "manufacturing", "security",
    ],
}

# Title exclusions — applied post-fetch since Apollo doesn't support NOT filters on titles
TITLE_EXCLUSIONS = [
    "business development", "sales", "coordinator", "intern",
    "content writer", "seo", "social media",
]

# Tier 1: decision-makers (primary targets)
PERSONA_TITLES_TIER1 = [
    "VP Demand Generation", "Head of Demand Gen", "VP Demand Gen",
    "VP ABM", "Head of ABM", "Director ABM", "Director of Account-Based Marketing",
    "VP Revenue Marketing", "Head of Revenue Marketing",
    "VP Marketing Operations", "Director Marketing Operations", "Director of Marketing Ops",
    "VP Field Marketing", "Director Field Marketing",
    "CMO", "Chief Marketing Officer",
]

# Tier 2: practitioners (multi-thread / volume)
PERSONA_TITLES_TIER2 = [
    "Demand Gen Manager", "Senior Demand Gen Manager",
    "ABM Manager", "Senior ABM Manager",
    "Marketing Operations Manager",
]

# Default: Tier 1 only. Pass PERSONA_TITLES_TIER1 + PERSONA_TITLES_TIER2 for practitioner sweeps.
PERSONA_TITLES = PERSONA_TITLES_TIER1


class ApolloClient:
    BASE = "https://api.apollo.io/api/v1"

    def __init__(self):
        self.api_key = get_required_setting(
            "FOLLOZE_APOLLO_API_KEY",
            keychain_service="apollo-folloze-api",
        )
        self.session = requests.Session()
        self.session.headers.update({
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        })

    def enrich_org(self, domain: str) -> Optional[dict]:
        """Company enrichment via Apollo organizations/enrich endpoint."""
        try:
            resp = self.session.get(
                f"{self.BASE}/organizations/enrich",
                params={"domain": domain},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            org = data.get("organization", {})
            if not org:
                return None

            tech_stack = []
            for tech in org.get("current_technologies", []):
                if isinstance(tech, dict):
                    tech_stack.append({
                        "name": tech.get("name", "Unknown"),
                        "category": tech.get("category", "Unknown"),
                    })
                else:
                    tech_stack.append({"name": str(tech), "category": "Unknown"})

            return {
                "name": org.get("name"),
                "domain": domain,
                "industry": org.get("industry"),
                "estimated_employees": org.get("estimated_num_employees"),
                "annual_revenue_printed": org.get("annual_revenue_printed"),
                "annual_revenue": org.get("annual_revenue"),
                "city": org.get("city"),
                "state": org.get("state"),
                "country": org.get("country"),
                "total_funding": org.get("total_funding"),
                "latest_funding_round_type": org.get("latest_funding_round_type"),
                "latest_funding_round_date": org.get("latest_funding_round_date"),
                "keywords": org.get("keywords", []),
                "tech_stack": tech_stack,
                "raw": org,
            }
        except requests.RequestException as e:
            print(f"[Apollo.enrich_org] {domain}: {e}")
            return None

    def search_companies(
        self,
        segment: int = 1,
        filters: Optional[dict] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict]:
        """
        Account universe search for ICP targeting via mixed_companies/search.
        segment=1: Software/SaaS 200-1,000 employees
        segment=2: Non-software verticals 500-10,000 employees
        See docs/ICP.md for full filter rationale.
        """
        base_filters = ICP_SEGMENT1_FILTERS if segment == 1 else ICP_SEGMENT2_FILTERS
        payload = {
            "page": page,
            "per_page": per_page,
            **base_filters,
            **(filters or {}),
        }

        try:
            resp = self.session.post(
                f"{self.BASE}/mixed_companies/search",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("organizations", [])
        except requests.RequestException as e:
            print(f"[Apollo.search_companies] segment={segment} page={page}: {e}")
            return []

    def search_people(
        self,
        domains: list[str],
        persona_titles: Optional[list[str]] = None,
        tier2: bool = False,
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict]:
        """
        Buying committee discovery via mixed_people/search.
        tier2=True includes practitioner titles (volume sweep).
        Title exclusions applied post-fetch per docs/ICP.md.
        """
        titles = persona_titles or (PERSONA_TITLES_TIER1 + PERSONA_TITLES_TIER2 if tier2 else PERSONA_TITLES_TIER1)
        payload = {
            "page": page,
            "per_page": per_page,
            "q_organization_domains_list": domains,
            "person_titles": titles,
            "person_seniorities": ["vp", "director", "c_suite", "head", "manager"],
        }

        try:
            resp = self.session.post(
                f"{self.BASE}/mixed_people/api_search",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            people = data.get("people", [])
            # api_search returns obfuscated profiles — normalize to expected shape.
            # Email reveal happens in reveal_emails job using bulk_match.
            normalized = []
            for p in people:
                title = p.get("title") or ""
                if any(ex in title.lower() for ex in TITLE_EXCLUSIONS):
                    continue
                normalized.append({
                    "apollo_id": p.get("id"),
                    "first_name": p.get("first_name"),
                    "last_name": None,           # obfuscated in api_search
                    "title": title,
                    "seniority": None,
                    "email": None,
                    "email_status": None,
                    "linkedin_url": None,
                })
            return normalized
        except requests.RequestException as e:
            print(f"[Apollo.search_people] domains={domains}: {e}")
            return []

    def match_by_linkedin(self, linkedin_urls: list[str]) -> dict[str, dict]:
        """
        Resolve contact profiles (including email) from a list of LinkedIn URLs.
        Uses people/bulk_match — only call for contacts with no existing email.
        Returns {linkedin_url: person_dict} for successful matches.
        Unmatched or error responses return an empty entry for that URL.
        """
        if not linkedin_urls:
            return {}
        payload = {"details": [{"linkedin_url": url} for url in linkedin_urls]}
        try:
            resp = self.session.post(
                f"{self.BASE}/people/bulk_match",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            matches = resp.json().get("matches", [])
            result = {}
            for url, match in zip(linkedin_urls, matches):
                if isinstance(match, dict) and match.get("email"):
                    result[url] = match
            return result
        except requests.RequestException as e:
            print(f"[Apollo.match_by_linkedin] {e}")
            return {}

    def reveal_emails(self, apollo_ids: list[str]) -> dict[str, dict]:
        """
        Bulk-reveal full contact profiles (email, last_name, linkedin_url) for
        a list of Apollo person IDs via people/bulk_match.
        Returns {apollo_id: person_dict}.
        """
        if not apollo_ids:
            return {}
        payload = {"details": [{"id": aid} for aid in apollo_ids]}
        try:
            resp = self.session.post(
                f"{self.BASE}/people/bulk_match",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            matches = resp.json().get("matches", [])
            revealed = {}
            for match in matches:
                if not isinstance(match, dict):
                    continue
                apollo_id = match.get("id")
                if apollo_id:
                    revealed[apollo_id] = match
            return revealed
        except requests.RequestException as e:
            print(f"[Apollo.reveal_emails] {e}")
            return {}
