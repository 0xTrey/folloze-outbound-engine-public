"""
Smartlead API wrapper for the Folloze outbound engine.
"""
from __future__ import annotations

import os
import re
import time
import requests
from typing import Optional

from runtime_config import get_required_setting


class SmartleadClient:
    BASE = "https://server.smartlead.ai/api/v1"
    # Smartlead rate limit: 10 requests/second on Pro plan
    _RATE_LIMIT_DELAY = 0.12  # seconds between calls

    def __init__(self):
        self.api_key = get_required_setting(
            "FOLLOZE_SMARTLEAD_API_KEY",
            keychain_service="smartlead-api",
        )
        if not self.api_key:
            raise RuntimeError(
                "FOLLOZE_SMARTLEAD_API_KEY not set. Load scripts/load_env.sh first."
            )
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    @staticmethod
    def _redacted_url(url: str) -> str:
        return re.sub(r"([?&]api_key=)[^&]+", r"\1<redacted>", url)

    def _raise_for_status(self, resp: requests.Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            safe_url = self._redacted_url(resp.url)
            raise requests.HTTPError(
                f"{resp.status_code} error from Smartlead for url: {safe_url}",
                response=resp,
            ) from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        params["api_key"] = self.api_key
        resp = self.session.get(f"{self.BASE}{path}", params=params, timeout=30)
        self._raise_for_status(resp)
        time.sleep(self._RATE_LIMIT_DELAY)
        return resp.json()

    def _post(self, path: str, payload: dict, params: dict | None = None) -> dict:
        params = params or {}
        params["api_key"] = self.api_key
        resp = self.session.post(f"{self.BASE}{path}", json=payload, params=params, timeout=30)
        self._raise_for_status(resp)
        time.sleep(self._RATE_LIMIT_DELAY)
        return resp.json()

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------

    def list_campaigns(self) -> list[dict]:
        """List all campaigns. Returns list of campaign objects."""
        data = self._get("/campaigns")
        return data if isinstance(data, list) else data.get("data", [])

    def get_campaign(self, campaign_id: str) -> dict:
        """Get campaign details by ID."""
        return self._get(f"/campaigns/{campaign_id}")

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    def add_leads(
        self,
        campaign_id: str,
        leads: list[dict],
        settings: Optional[dict] = None,
    ) -> dict:
        """
        Add one or more leads to a campaign.

        Each lead dict should contain:
            first_name, last_name, email (required)
            company_name, website, phone_number (optional)
            custom_fields: dict of key→value for template variables

        settings dict (optional):
            ignore_global_block_list: bool (default False)
            ignore_unsubscribe_list: bool (default False)
            ignore_community_bounce_list: bool (default False)

        Returns Smartlead response with ok/error info per lead.
        """
        payload = {
            "lead_list": leads,
            "settings": settings or {
                "ignore_global_block_list": False,
                "ignore_unsubscribe_list": False,
                "ignore_community_bounce_list": False,
            },
        }
        return self._post(f"/campaigns/{campaign_id}/leads", payload)

    def get_leads(
        self,
        campaign_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        """
        Fetch leads from a campaign. Returns list of campaign-lead-map objects.
        Each item has: campaign_lead_map_id, status, created_at, lead (sub-object with
        email, is_unsubscribed, custom_fields, etc.)
        Used by deliverability (unsub/bounce sync) and pull_events.
        """
        data = self._get(
            f"/campaigns/{campaign_id}/leads",
            params={"offset": offset, "limit": limit},
        )
        if isinstance(data, list):
            return data
        # API returns {"total_leads": "N", "data": [...]}
        return data.get("data", [])

    def get_lead_by_email(self, email: str) -> Optional[dict]:
        """Look up a lead by email across all campaigns."""
        try:
            return self._get(f"/leads", params={"email": email})
        except requests.HTTPError:
            return None

    # ------------------------------------------------------------------
    # Email accounts (mailboxes)
    # ------------------------------------------------------------------

    def list_email_accounts(self) -> list[dict]:
        """
        List all email accounts (sending mailboxes) connected to the Smartlead workspace.
        Returns list of accounts with id, email, warmup_status, daily_limit, etc.
        """
        data = self._get("/email-accounts")
        return data if isinstance(data, list) else data.get("data", [])

    def get_email_account(self, account_id: str) -> dict:
        """Get a single email account's details and current health."""
        return self._get(f"/email-accounts/{account_id}")

    def get_email_account_warmup_stats(self, account_id: str) -> dict:
        """Get 7-day warmup statistics for one email account."""
        return self._get(f"/email-accounts/{account_id}/warmup-stats")

    def save_email_account(self, payload: dict) -> dict:
        """
        Create or update an email account.

        Smartlead uses the same endpoint for both create and update flows. Pass
        `id` in the payload to update an existing account.
        """
        return self._post("/email-accounts/save", payload)

    def update_email_account(self, account_id: str, payload: dict) -> dict:
        """Update non-credential settings for an existing email account."""
        return self._post(f"/email-accounts/{account_id}", payload)

    def update_email_account_warmup(self, account_id: str, payload: dict) -> dict:
        """Update dedicated warmup settings for an existing email account."""
        return self._post(f"/email-accounts/{account_id}/warmup", payload)

    def configure_email_account_warmup(
        self,
        account_id: str,
        *,
        warmup_enabled: bool,
        total_warmup_per_day: int,
        daily_rampup: int,
        max_email_per_day: int,
        reply_rate_percentage: int,
        time_to_wait_in_mins: int = 20,
        auto_adjust_warmup: bool = True,
        is_rampup_enabled: bool = True,
    ) -> dict:
        """
        Update warmup-related settings for an existing email account.

        Smartlead exposes separate endpoints for general email-account settings
        and warmup controls. This method updates both so the account's daily
        cap and warmup ramp settings stay aligned.
        """
        self.update_email_account(
            account_id,
            {
                "max_email_per_day": int(max_email_per_day),
                "time_to_wait_in_mins": int(time_to_wait_in_mins),
            },
        )
        return self.update_email_account_warmup(
            account_id,
            {
                "warmup_enabled": bool(warmup_enabled),
                "total_warmup_per_day": int(total_warmup_per_day),
                "daily_rampup": int(daily_rampup),
                "reply_rate_percentage": int(reply_rate_percentage),
                "auto_adjust_warmup": bool(auto_adjust_warmup),
                "is_rampup_enabled": bool(is_rampup_enabled),
            },
        )

    # ------------------------------------------------------------------
    # Campaign email accounts (inbox rotation)
    # ------------------------------------------------------------------

    def get_campaign_email_accounts(self, campaign_id: str) -> list[dict]:
        """
        List the email accounts assigned to a campaign.
        Smartlead rotates sends across all assigned accounts automatically.
        """
        data = self._get(f"/campaigns/{campaign_id}/email-accounts")
        return data if isinstance(data, list) else data.get("data", [])

    # ------------------------------------------------------------------
    # Analytics / telemetry
    # ------------------------------------------------------------------

    def get_campaign_stats(self, campaign_id: str) -> dict:
        """
        Get aggregate stats for a campaign.
        Returns normalized dict with int fields:
            sent_count, open_count, click_count, reply_count, block_count, total_count
        """
        raw = self._get(f"/campaigns/{campaign_id}/analytics")
        return {
            "sent_count":  int(raw.get("sent_count") or 0),
            "open_count":  int(raw.get("open_count") or 0),
            "click_count": int(raw.get("click_count") or 0),
            "reply_count": int(raw.get("reply_count") or 0),
            "block_count": int(raw.get("block_count") or 0),
            "total_count": int(raw.get("total_count") or 0),
        }

    def get_campaign_statistics_page(
        self,
        campaign_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """
        Fetch per-send statistics rows for a campaign.

        Returns the raw JSON payload with:
            total_stats, offset, limit, data[]

        Each data row represents one sent email instance and includes fields such as:
            lead_email, sent_time, open_time, click_time, reply_time,
            is_bounced, is_unsubscribed, email_campaign_seq_id, email_subject.
        """
        data = self._get(
            f"/campaigns/{campaign_id}/statistics",
            params={"offset": offset, "limit": limit},
        )
        return data if isinstance(data, dict) else {"data": data or []}

    def get_lead_message_history(self, campaign_id: str, lead_id: str) -> list[dict]:
        """Get the full send + reply history for one lead in a campaign."""
        data = self._get(f"/campaigns/{campaign_id}/leads/{lead_id}/message-history")
        return data if isinstance(data, list) else data.get("data", [])

    # ------------------------------------------------------------------
    # Campaign provisioning (setup_campaigns job)
    # ------------------------------------------------------------------

    def create_campaign(self, name: str) -> dict:
        """
        Create a new campaign. Returns campaign object with 'id' field.
        Only name is accepted at creation; use update_campaign_settings for behavior config.
        """
        return self._post("/campaigns/create", {"name": name})

    def update_campaign_settings(self, campaign_id: str, settings: dict) -> dict:
        """
        Update settings for an existing campaign.

        Valid fields (confirmed against API):
            send_as_plain_text: bool
            stop_lead_settings: str ("REPLY_TO_AN_EMAIL" | "CLICK_ON_A_LINK" | "OPEN_AN_EMAIL")
            follow_up_percentage: int (1-100, percentage of leads to follow up with)
            unsubscribe_text: str (custom unsubscribe footer text)
            enable_ai_esp_matching: bool
        """
        return self._post(f"/campaigns/{campaign_id}/settings", settings)

    def create_sequence(self, campaign_id: str, sequences: list[dict]) -> dict:
        """
        Create sequence steps for a campaign.

        Each sequence dict:
            seq_number: int (1-indexed)
            subject: str (empty string for threaded replies)
            email_body: str (HTML or plain text; supports {{variable}} placeholders)
            seq_delay_details: dict with delay_in_days (int)

        Steps 2-3 should have empty subject (Smartlead threads them as replies when subject is "").
        Steps 4-5 should have a new subject (new thread).

        Endpoint: POST /campaigns/{id}/sequences
        Returns Smartlead response.
        """
        return self._post(
            f"/campaigns/{campaign_id}/sequences",
            {"sequences": sequences},
        )

    def set_campaign_schedule(
        self,
        campaign_id: str,
        days_of_the_week: list[int] | None = None,
        start_hour: str = "09:00",
        end_hour: str = "16:00",
        timezone: str = "America/Chicago",
        min_time_btw_emails: int = 30,
        max_new_leads_per_day: int = 50,
    ) -> dict:
        """
        Set the send schedule for a campaign. Required before activation.

        days_of_the_week: list of ints 1-7 (Mon=1). Defaults to Mon-Fri.
        start_hour / end_hour: "HH:MM" 24h format.
        min_time_btw_emails: minutes between sends per mailbox.
        max_new_leads_per_day: new leads to start per day.
        """
        payload = {
            "days_of_the_week": days_of_the_week or [1, 2, 3, 4, 5],
            "start_hour": start_hour,
            "end_hour": end_hour,
            "timezone": timezone,
            "min_time_btw_emails": min_time_btw_emails,
            "max_new_leads_per_day": max_new_leads_per_day,
        }
        return self._post(f"/campaigns/{campaign_id}/schedule", payload)

    def start_campaign(self, campaign_id: str) -> dict:
        """Activate a campaign (status DRAFTED → ACTIVE). Schedule must be set first."""
        return self._post(f"/campaigns/{campaign_id}/status", {"status": "START"})

    def assign_email_accounts_to_campaign(
        self,
        campaign_id: str,
        email_account_ids: list[int],
    ) -> dict:
        """
        Assign sending mailboxes to a campaign for inbox rotation.
        Smartlead distributes sends across all assigned accounts automatically.

        email_account_ids: list of Smartlead email account IDs (integers).
        """
        return self._post(
            f"/campaigns/{campaign_id}/email-accounts",
            {"email_account_ids": email_account_ids},
        )

    # ------------------------------------------------------------------
    # Unsubscribes / bounces
    # ------------------------------------------------------------------

    def add_to_block_list(self, emails: list[str]) -> dict:
        """Add emails to the global block list (hard unsubscribes)."""
        return self._post("/block-list", {"emails": emails})

    # ------------------------------------------------------------------
    # Sequence management (template updates)
    # ------------------------------------------------------------------

    def get_sequences(self, campaign_id: str) -> list[dict]:
        """
        Fetch the sequence steps for a campaign.
        Returns list of steps with id, seq_number, subject, email_body, seq_delay_details.
        """
        data = self._get(f"/campaigns/{campaign_id}/sequences")
        if isinstance(data, list):
            return data
        return data.get("data", [])

    def replace_sequences(self, campaign_id: str, sequences: list[dict]) -> dict:
        """
        Replace all sequence steps for a campaign in one call.
        POST /campaigns/{id}/sequences overwrites the entire sequence list.
        Pass all 5 steps — partial posts will delete the others.
        Identical to create_sequence but named clearly to reflect the replace semantics.
        """
        return self._post(
            f"/campaigns/{campaign_id}/sequences",
            {"sequences": sequences},
        )
