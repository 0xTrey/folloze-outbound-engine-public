"""
X (Twitter) API client for contact signal research.

Uses Bearer token (read-only search) — sufficient for prospecting lookups.
Requires X_BEARER_TOKEN in environment (Keychain: x-api-bearer-token).

Rate limits (Basic tier):
  - Recent search: 60 requests / 15 min
  - User lookup: 15 requests / 15 min
  - ~10k tweets/month cap on recent search endpoint

Use search_contact_posts() when you only have name + company.
Use get_user_recent_posts() when you have the prospect's X handle (more precise).
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests
from runtime_config import get_required_setting

X_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
X_USER_BY_USERNAME_URL = "https://api.twitter.com/2/users/by/username/{username}"
X_USER_TWEETS_URL = "https://api.twitter.com/2/users/{user_id}/tweets"

REQUEST_DELAY = 1.5  # conservative: ~40 req/min vs 60 allowed


class XClient:
    def __init__(self, bearer_token: Optional[str] = None):
        self.bearer_token = bearer_token or get_required_setting(
            "FOLLOZE_X_BEARER_TOKEN",
            keychain_service="x-api-bearer-token",
            allow_empty=True,
        )
        if not self.bearer_token:
            raise ValueError(
                "FOLLOZE_X_BEARER_TOKEN not set. Load scripts/load_env.sh first."
            )
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_call = time.time()

    def _get(self, url: str, params: dict) -> dict:
        self._throttle()
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.bearer_token}"},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def search_contact_posts(
        self,
        first_name: str,
        last_name: str,
        company: str,
        max_results: int = 5,
    ) -> list[dict]:
        """
        Search for recent public posts related to a prospect based on name + company.
        Works even without knowing their X handle.

        Returns list of:
        {
            "text": str,
            "created_at": str,
            "id": str,
        }
        Returns empty list on failure rather than raising.
        """
        # Keyword search scoped to company + marketing topics.
        # Not tied to a specific account — casts a wider net.
        query = (
            f'"{company}" '
            f'(marketing OR "demand gen" OR ABM OR "go-to-market" OR GTM OR "revenue marketing") '
            f'-is:retweet lang:en'
        )
        try:
            data = self._get(
                X_SEARCH_URL,
                {
                    "query": query,
                    "max_results": min(max(max_results, 10), 100),  # API min is 10
                    "tweet.fields": "created_at,text,author_id",
                    "sort_order": "recency",
                },
            )
            results = []
            for t in (data.get("data") or []):
                text = t.get("text", "")
                # Filter: only include if the contact's name appears in the tweet
                # (best-effort — not guaranteed to be from them, but a good signal)
                if first_name.lower() in text.lower() or last_name.lower() in text.lower():
                    results.append({
                        "text": text,
                        "created_at": t.get("created_at", ""),
                        "id": t.get("id", ""),
                    })
            return results[:max_results]
        except Exception as e:
            print(
                f"[x] search_contact_posts failed for {first_name} {last_name} @ {company}: {e}"
            )
            return []

    def get_user_recent_posts(
        self,
        username: str,
        max_results: int = 5,
    ) -> list[dict]:
        """
        Look up a user by their X handle and return recent non-retweet posts.
        Use this when you have the prospect's handle (e.g., from Apollo or LinkedIn enrichment).

        Returns list of:
        {
            "text": str,
            "created_at": str,
            "id": str,
        }
        Returns empty list on failure.
        """
        handle = username.lstrip("@")
        try:
            user_data = self._get(
                X_USER_BY_USERNAME_URL.format(username=handle),
                {"user.fields": "id,name"},
            )
            user_id = (user_data.get("data") or {}).get("id")
            if not user_id:
                print(f"[x] user not found: @{handle}")
                return []

            tweets_data = self._get(
                X_USER_TWEETS_URL.format(user_id=user_id),
                {
                    "max_results": min(max(max_results, 5), 100),  # API min is 5
                    "tweet.fields": "created_at,text",
                    "exclude": "retweets,replies",
                },
            )
            return [
                {
                    "text": t.get("text", ""),
                    "created_at": t.get("created_at", ""),
                    "id": t.get("id", ""),
                }
                for t in (tweets_data.get("data") or [])
            ][:max_results]
        except Exception as e:
            print(f"[x] get_user_recent_posts failed for @{handle}: {e}")
            return []
