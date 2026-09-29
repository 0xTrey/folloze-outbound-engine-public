"""
Perplexity API client for company signal enrichment.

Requires PERPLEXITY_API_KEY in environment.
Rate limit: conservative 1 req/sec (free tier is 5 req/min, Pro is higher).
Model: sonar (online search, fast, cheap ~$1/1M tokens)
"""
from __future__ import annotations

import json
import os
import re
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
    from api_tracker.telemetry import append_event, build_perplexity_usage
except Exception:
    def append_event(event):
        return None

    def build_perplexity_usage(payload, **kwargs):
        return None

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "sonar"  # online search model; upgrade to sonar-pro for deeper research
REQUEST_DELAY = 1.1  # seconds between calls (conservative)


class PerplexityClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_required_setting(
            "FOLLOZE_PERPLEXITY_API_KEY",
            keychain_service="perplexity-api",
        )
        if not self.api_key:
            raise ValueError(
                "FOLLOZE_PERPLEXITY_API_KEY not set. Load scripts/load_env.sh first."
            )
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_call = time.time()

    def _post(self, messages: list[dict], **kwargs) -> dict:
        self._throttle()
        payload = {
            "model": MODEL,
            "messages": messages,
            "search_recency_filter": "month",  # only index last ~30 days
            "return_citations": False,
            "temperature": 0.1,
            **kwargs,
        }
        resp = requests.post(
            PERPLEXITY_API_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        append_event(build_perplexity_usage(payload))
        return payload

    def enrich_company(self, company_name: str, domain: str) -> dict:
        """
        Query Perplexity for recent signals about a company relevant to
        B2B demand generation / marketing persona.

        Returns a dict:
        {
            "summary": str,          # 1-2 sentence company snapshot
            "signals": [             # ordered by relevance
                {"type": str, "text": str},
                ...
            ]
        }

        Signal types: funding | hiring | product | leadership | event | partnership | award
        Returns empty signals list on failure rather than raising.
        """
        system = (
            "You extract recent B2B company intelligence for a sales prospecting tool. "
            "Return ONLY valid JSON in this exact shape:\n"
            '{"summary": "...", "signals": [{"type": "...", "text": "..."}]}\n\n'
            "Rules:\n"
            "- summary: 1-2 sentences max. Company size, market, and one key recent development.\n"
            "- signals: up to 5 items. Each must be a specific, factual, recent development "
            "(last 90 days preferred). Signal types: funding | hiring | product | leadership | "
            "event | partnership | award.\n"
            "- Focus on signals relevant to a demand generation or marketing leader at the company.\n"
            "- If no recent signals exist, return an empty signals array.\n"
            "- Do NOT invent signals. Only include what you can verify from search results.\n"
            "- Output ONLY the JSON object. No preamble, no markdown."
        )

        user = (
            f"Company: {company_name} (domain: {domain})\n\n"
            f"Find recent news and signals about {company_name} from the last 90 days. "
            f"Focus on: funding rounds, leadership changes, product launches, AI/marketing "
            f"technology announcements, major partnerships, hiring surges (especially in marketing "
            f"or demand gen), and industry awards. Return as JSON per instructions."
        )

        try:
            response = self._post([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            text = response["choices"][0]["message"]["content"].strip()

            # Strip markdown fences if present
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text.strip())

            parsed = json.loads(text)
            return {
                "summary": parsed.get("summary") or "",
                "signals": parsed.get("signals") or [],
            }

        except Exception as e:
            print(f"[perplexity] enrich_company failed for {company_name}: {e}")
            return {"summary": "", "signals": []}

    def research_contact(
        self,
        first_name: str,
        last_name: str,
        title: str,
        company: str,
    ) -> dict:
        """
        Research an individual contact for personalization hooks.
        Finds recent public activity — posts, talks, articles, quotes, job changes.

        Returns:
        {
            "summary": str,   # 1-2 sentence description of who they are
            "signals": [
                {"type": "post|talk|article|quote|hire|award", "text": str},
                ...
            ]
        }
        Returns empty signals list on failure rather than raising.
        """
        system = (
            "You extract recent public intelligence about a B2B professional for cold outbound personalization. "
            "Return ONLY valid JSON in this exact shape:\n"
            '{"summary": "...", "signals": [{"type": "...", "text": "..."}]}\n\n'
            "Rules:\n"
            "- summary: 1-2 sentences max. Who they are, their focus area, and their seniority.\n"
            "- signals: up to 4 items. Each must be a specific, factual, recent activity or public statement. "
            "Signal types: post | talk | article | quote | hire | award.\n"
            "- Focus on: LinkedIn articles or posts they authored, conference talks or panels (last 12 months), "
            "quotes in industry press, recent job change, awards or recognition.\n"
            "- Do NOT invent signals. Only include what you can verify from search results.\n"
            "- If no relevant public signals exist, return an empty signals array.\n"
            "- Output ONLY the JSON object. No preamble, no markdown."
        )

        user = (
            f"Person: {first_name} {last_name}\n"
            f"Title: {title}\n"
            f"Company: {company}\n\n"
            f"Find recent public signals about {first_name} {last_name} from {company} "
            f"that would be useful for personalizing a cold outbound email from a B2B marketing software company. "
            f"Look for: LinkedIn articles they wrote, conference talks, press quotes, recent role changes, "
            f"or any public statements about their marketing or demand gen strategy. "
            f"Return as JSON per instructions."
        )

        try:
            response = self._post([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            text = response["choices"][0]["message"]["content"].strip()

            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text.strip())

            parsed = json.loads(text)
            return {
                "summary": parsed.get("summary") or "",
                "signals": parsed.get("signals") or [],
            }

        except Exception as e:
            print(f"[perplexity] research_contact failed for {first_name} {last_name} @ {company}: {e}")
            return {"summary": "", "signals": []}
