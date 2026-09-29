"""
Brave Search API client for web research fallback.

Used when Perplexity returns thin results for smaller/newer companies,
or when a company lacks strong coverage in Perplexity's index.

Requires BRAVE_API_KEY in environment (Keychain: brave-search-api).
Free tier: 2,000 queries/month. Rate limit: 1 req/sec.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from runtime_config import get_required_setting

TRACKER_COLLECTOR_ROOT = Path(os.getenv("FOLLOZE_API_TRACKER_COLLECTOR_ROOT", "")).expanduser()
if str(TRACKER_COLLECTOR_ROOT) != "." and TRACKER_COLLECTOR_ROOT.exists() and str(TRACKER_COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(TRACKER_COLLECTOR_ROOT))

try:
    from api_tracker.telemetry import append_event, build_brave_snapshot
except Exception:
    def append_event(event):
        return None

    def build_brave_snapshot(headers, **kwargs):
        return None

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
REQUEST_DELAY = 1.1  # seconds between calls (conservative, free tier)


class BraveClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_required_setting(
            "FOLLOZE_BRAVE_API_KEY",
            keychain_service="brave-search-api",
        )
        if not self.api_key:
            raise ValueError(
                "FOLLOZE_BRAVE_API_KEY not set. Load scripts/load_env.sh first."
            )
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_call = time.time()

    def search(self, query: str, count: int = 5) -> list[dict]:
        """
        Run a web search and return structured results.

        Returns list of:
        {
            "title": str,
            "url": str,
            "description": str,
        }
        Returns empty list on failure rather than raising.
        """
        self._throttle()
        try:
            resp = requests.get(
                BRAVE_SEARCH_URL,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.api_key,
                },
                params={
                    "q": query,
                    "count": min(count, 20),  # max 20 per request
                    "freshness": "pm",         # past month
                    "text_decorations": False,
                    "search_lang": "en",
                    "country": "US",
                },
                timeout=15,
            )
            resp.raise_for_status()
            append_event(build_brave_snapshot(resp.headers))
            data = resp.json()
            results = data.get("web", {}).get("results") or []
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                }
                for r in results[:count]
            ]
        except Exception as e:
            print(f"[brave] search failed for query '{query}': {e}")
            return []

    def research_company(self, company_name: str, domain: str) -> list[dict]:
        """
        Search for recent company signals relevant to B2B demand gen outreach.
        Excludes LinkedIn to avoid gating issues.

        Returns up to 5 results (title, url, description).
        """
        query = (
            f'"{company_name}" '
            f'(marketing OR "demand generation" OR "revenue marketing" '
            f'OR funding OR "go-to-market" OR GTM OR hiring) '
            f'-site:linkedin.com'
        )
        return self.search(query, count=5)

    def research_contact(
        self,
        first_name: str,
        last_name: str,
        company: str,
    ) -> list[dict]:
        """
        Search for public signals about a contact — articles, talks, press quotes.
        Returns up to 3 results.
        """
        query = (
            f'"{first_name} {last_name}" "{company}" '
            f'(marketing OR "demand gen" OR ABM OR "revenue marketing" OR GTM)'
        )
        return self.search(query, count=3)
