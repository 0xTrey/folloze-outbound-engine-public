"""
Salesforce CRM suppression client.
Uses SF CLI subprocess calls (org alias: folloze) to pull customer and open-opp domains.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date
from urllib.parse import urlparse

from db.connection import db_conn

SF_TARGET_ORG = "folloze"
OUTBOUND_LEAD_SOURCE = "Other"
OUTBOUND_LEAD_SOURCE_DETAIL = "Apollo - Folloze outbound engine"


def _clean_domain(url: str) -> str:
    """Strip protocol, www., paths, and trailing slashes — return bare domain."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("/")[0].lower()
    return domain


def _escape_sf_value(value: str) -> str:
    """Escape values for SF CLI --values payloads."""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


class CRMClient:
    def __init__(self) -> None:
        self._account_cache: dict[tuple[str, str, str], str | None] = {}

    def _run_sf_cmd(self, args: list[str]) -> subprocess.CompletedProcess:
        # SF_AUTOUPDATE_DISABLE prevents the CLI from printing an update warning
        # to stderr and exiting with code 2, which would kill the pipeline.
        # NODE_PATH override: SF CLI crashes on Node.js v25 (ERR_INVALID_STATE on
        # FileHandle GC). Prepend node@22 so `sf` picks up the LTS runtime.
        env = os.environ.copy()
        env["SF_AUTOUPDATE_DISABLE"] = "true"
        node22_bin = "/opt/homebrew/opt/node@22/bin"
        env["PATH"] = f"{node22_bin}:{env.get('PATH', '')}"
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(
                f"SF CLI failed (exit {result.returncode})\n"
                f"stderr: {result.stderr.strip()}\n"
                f"stdout: {result.stdout.strip()}"
            )
        return result

    def _parse_sf_json(self, result: subprocess.CompletedProcess) -> dict:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"SF CLI returned non-JSON: {result.stdout[:200]!r}"
            ) from exc

    def _run_sf_query(self, soql: str) -> list[dict]:
        result = self._run_sf_cmd(
            ["sf", "data", "query", "--target-org", SF_TARGET_ORG, "--json", "--query", soql]
        )
        return self._parse_sf_json(result)["result"]["records"]

    def _run_sf_create(self, sobject: str, values: dict[str, str]) -> dict:
        values_arg = " ".join(
            f"{field}='{_escape_sf_value(value)}'"
            for field, value in values.items()
            if value not in (None, "")
        )
        result = self._run_sf_cmd(
            [
                "sf", "data", "create", "record",
                "--target-org", SF_TARGET_ORG,
                "--json",
                "--sobject", sobject,
                "--values", values_arg,
            ]
        )
        return self._parse_sf_json(result).get("result", {})

    def _run_sf_update(self, sobject: str, record_id: str, values: dict[str, str]) -> dict:
        values_arg = " ".join(
            f"{field}='{_escape_sf_value(value)}'"
            for field, value in values.items()
            if value not in (None, "")
        )
        result = self._run_sf_cmd(
            [
                "sf", "data", "update", "record",
                "--target-org", SF_TARGET_ORG,
                "--json",
                "--sobject", sobject,
                "--record-id", record_id,
                "--values", values_arg,
            ]
        )
        return self._parse_sf_json(result).get("result", {})

    def _match_account_candidate(
        self,
        records: list[dict],
        *,
        domain: str,
        company_name: str,
    ) -> str | None:
        normalized_domain = _clean_domain(domain or "")
        normalized_name = (company_name or "").strip().lower()

        exact_domain = None
        exact_name = None
        first = None
        for record in records:
            record_id = record.get("Id")
            if not record_id:
                continue
            first = first or record_id
            website = _clean_domain(record.get("Website") or "")
            name = (record.get("Name") or "").strip().lower()
            if normalized_domain and website == normalized_domain:
                exact_domain = record_id
                break
            if normalized_name and name == normalized_name:
                exact_name = exact_name or record_id
        return exact_domain or exact_name or first

    def find_account_id(
        self,
        *,
        domain: str = "",
        company_name: str = "",
        crm_account_id: str = "",
    ) -> str | None:
        key = (
            (crm_account_id or "").strip(),
            _clean_domain(domain or ""),
            (company_name or "").strip().lower(),
        )
        if key in self._account_cache:
            return self._account_cache[key]

        if crm_account_id:
            self._account_cache[key] = crm_account_id
            return crm_account_id

        filters = []
        if key[1]:
            filters.append(f"Website LIKE '%{_escape_sf_value(key[1])}%'")
        if company_name:
            filters.append(f"Name = '{_escape_sf_value(company_name)}'")
        if not filters:
            self._account_cache[key] = None
            return None

        records = self._run_sf_query(
            "SELECT Id, Name, Website FROM Account "
            f"WHERE {' OR '.join(filters)} LIMIT 10"
        )
        match = self._match_account_candidate(
            records,
            domain=domain,
            company_name=company_name,
        )
        self._account_cache[key] = match
        return match

    def _safe_last_name(self, first_name: str, last_name: str, email: str) -> str:
        if last_name:
            return last_name
        if first_name:
            return first_name
        if email and "@" in email:
            return email.split("@", 1)[0][:80]
        return "Outbound Prospect"

    def _build_sync_description(self, contact: dict) -> str:
        lines = ["Managed by Folloze outbound engine sync."]

        company_name = contact.get("company_name") or ""
        domain = contact.get("domain") or ""
        if company_name or domain:
            lines.append(f"Company: {company_name or domain}")
        if domain:
            lines.append(f"Domain: {domain}")

        title = contact.get("title") or ""
        if title:
            lines.append(f"Title: {title}")

        persona_type = contact.get("persona_type") or ""
        if persona_type:
            lines.append(f"Persona: {persona_type}")

        segment = contact.get("segment") or ""
        if segment:
            lines.append(f"Segment: {segment}")

        sequence_families = contact.get("sequence_families") or ""
        if sequence_families:
            lines.append(f"Sequence families: {sequence_families}")

        linkedin_url = contact.get("linkedin_url") or ""
        if linkedin_url:
            lines.append(f"LinkedIn: {linkedin_url}")

        source = contact.get("source") or ""
        if source:
            lines.append(f"Source: {source}")

        email_status = contact.get("email_status") or ""
        if email_status:
            lines.append(f"Email status: {email_status}")

        last_enriched_at = contact.get("last_enriched_at")
        if last_enriched_at:
            lines.append(f"Last enriched: {last_enriched_at}")

        return "\n".join(lines)

    def _build_contact_sync_values(self, contact: dict, account_id: str | None) -> dict[str, str]:
        first_name = (contact.get("first_name") or "").strip()
        last_name = self._safe_last_name(
            first_name,
            (contact.get("last_name") or "").strip(),
            (contact.get("email") or "").strip(),
        )
        values = {
            "FirstName": first_name,
            "LastName": last_name,
            "Email": (contact.get("email") or "").strip(),
            "Title": (contact.get("title") or "").strip(),
            "Department": (contact.get("department") or "").strip(),
            "LeadSource": OUTBOUND_LEAD_SOURCE,
            "Lead_Source_Detail__c": OUTBOUND_LEAD_SOURCE_DETAIL,
            "Description": self._build_sync_description(contact),
        }
        if account_id:
            values["AccountId"] = account_id
        return values

    def _build_lead_sync_values(self, contact: dict) -> dict[str, str]:
        first_name = (contact.get("first_name") or "").strip()
        company_name = (contact.get("company_name") or contact.get("domain") or "Unknown").strip()
        return {
            "FirstName": first_name,
            "LastName": self._safe_last_name(
                first_name,
                (contact.get("last_name") or "").strip(),
                (contact.get("email") or "").strip(),
            ),
            "Company": company_name,
            "Email": (contact.get("email") or "").strip(),
            "Title": (contact.get("title") or "").strip(),
            "LeadSource": OUTBOUND_LEAD_SOURCE,
            "Lead_Source_Detail__c": OUTBOUND_LEAD_SOURCE_DETAIL,
            "Description": self._build_sync_description(contact),
        }

    def _normalize_value(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    def _diff_fields(self, existing: dict, desired: dict[str, str]) -> dict[str, str]:
        updates = {}
        for field, value in desired.items():
            if self._normalize_value(existing.get(field)) != self._normalize_value(value):
                updates[field] = value
        return updates

    def get_customer_domains(self) -> set[str]:
        """Return domains of active Salesforce customers."""
        records = self._run_sf_query(
            "SELECT Website FROM Account WHERE Type = 'Customer' AND Website != null"
        )
        domains = set()
        for r in records:
            d = _clean_domain(r.get("Website") or "")
            if d:
                domains.add(d)
        return domains

    def get_open_opp_domains(self) -> set[str]:
        """Return domains with open Salesforce opportunities."""
        records = self._run_sf_query(
            "SELECT Account.Website FROM Opportunity "
            "WHERE IsClosed = false AND Account.Website != null"
        )
        domains = set()
        for r in records:
            acct = r.get("Account") or {}
            d = _clean_domain(acct.get("Website") or "")
            if d:
                domains.add(d)
        return domains

    def find_person_by_email(self, email: str) -> dict | None:
        if not email:
            return None

        safe_email = _escape_sf_value(email)
        queries = (
            (
                "Contact",
                "SELECT Id, Name, FirstName, LastName, Email, Title, Department, "
                "LeadSource, Lead_Source_Detail__c, Description, AccountId "
                "FROM Contact "
                f"WHERE Email = '{safe_email}' LIMIT 1",
            ),
            (
                "Lead",
                "SELECT Id, Name, FirstName, LastName, Company, Email, Title, "
                "LeadSource, Lead_Source_Detail__c, Description "
                "FROM Lead "
                f"WHERE Email = '{safe_email}' LIMIT 1",
            ),
        )

        for kind, soql in queries:
            records = self._run_sf_query(soql)
            if records:
                record = records[0]
                record["_sf_type"] = kind
                return record
        return None

    def get_people_by_ids(self, person_ids: list[str]) -> dict[str, dict]:
        """Return Salesforce Contact/Lead records keyed by Id for the given ids."""
        unique_ids = [pid for pid in dict.fromkeys(person_ids) if pid]
        if not unique_ids:
            return {}

        def _batched(values: list[str], size: int = 100) -> list[list[str]]:
            return [values[idx:idx + size] for idx in range(0, len(values), size)]

        contacts = [pid for pid in unique_ids if pid.startswith("003")]
        leads = [pid for pid in unique_ids if pid.startswith("00Q")]
        found: dict[str, dict] = {}

        for batch in _batched(contacts):
            quoted = ", ".join(f"'{_escape_sf_value(pid)}'" for pid in batch)
            records = self._run_sf_query(
                "SELECT Id, Email, AccountId, FirstName, LastName, Name, Title "
                "FROM Contact "
                f"WHERE Id IN ({quoted})"
            )
            for record in records:
                record["_sf_type"] = "Contact"
                found[record["Id"]] = record

        for batch in _batched(leads):
            quoted = ", ".join(f"'{_escape_sf_value(pid)}'" for pid in batch)
            records = self._run_sf_query(
                "SELECT Id, Email, Company, FirstName, LastName, Name, Title "
                "FROM Lead "
                f"WHERE Id IN ({quoted})"
            )
            for record in records:
                record["_sf_type"] = "Lead"
                found[record["Id"]] = record

        return found

    def sync_outbound_contact(self, contact: dict) -> dict:
        """
        Sync one outbound-engine contact into Salesforce.

        Behavior:
        - Existing Contact -> update key fields and AccountId when changed.
        - Existing Lead -> update key fields when changed.
        - Missing person + resolvable Account -> create Contact.
        - Missing person + no Account -> create Lead.
        """
        email = (contact.get("email") or "").strip()
        if not email:
            return {
                "action": "skipped_no_email",
                "record_id": None,
                "matched_type": None,
                "matched_id": None,
            }

        person = self.find_person_by_email(email)
        account_id = self.find_account_id(
            domain=contact.get("domain") or "",
            company_name=contact.get("company_name") or "",
            crm_account_id=contact.get("crm_account_id") or "",
        )

        if person and person.get("_sf_type") == "Contact":
            desired = self._build_contact_sync_values(
                contact,
                account_id or person.get("AccountId"),
            )
            updates = self._diff_fields(person, desired)
            if updates:
                self._run_sf_update("Contact", person["Id"], updates)
                return {
                    "action": "contact_updated",
                    "record_id": person["Id"],
                    "matched_type": "Contact",
                    "matched_id": person["Id"],
                    "updated_fields": sorted(updates.keys()),
                    "account_id": desired.get("AccountId"),
                }
            return {
                "action": "contact_unchanged",
                "record_id": person["Id"],
                "matched_type": "Contact",
                "matched_id": person["Id"],
                "updated_fields": [],
                "account_id": desired.get("AccountId"),
            }

        if person and person.get("_sf_type") == "Lead":
            desired = self._build_lead_sync_values(contact)
            updates = self._diff_fields(person, desired)
            if updates:
                self._run_sf_update("Lead", person["Id"], updates)
                return {
                    "action": "lead_updated",
                    "record_id": person["Id"],
                    "matched_type": "Lead",
                    "matched_id": person["Id"],
                    "updated_fields": sorted(updates.keys()),
                    "account_id": account_id,
                }
            return {
                "action": "lead_unchanged",
                "record_id": person["Id"],
                "matched_type": "Lead",
                "matched_id": person["Id"],
                "updated_fields": [],
                "account_id": account_id,
            }

        if account_id:
            values = self._build_contact_sync_values(contact, account_id)
            created = self._run_sf_create("Contact", values)
            return {
                "action": "contact_created",
                "record_id": created.get("id") or created.get("Id"),
                "matched_type": None,
                "matched_id": None,
                "updated_fields": sorted(values.keys()),
                "account_id": account_id,
            }

        values = self._build_lead_sync_values(contact)
        created = self._run_sf_create("Lead", values)
        return {
            "action": "lead_created",
            "record_id": created.get("id") or created.get("Id"),
            "matched_type": None,
            "matched_id": None,
            "updated_fields": sorted(values.keys()),
            "account_id": None,
        }

    def record_interested_reply(
        self,
        *,
        email: str,
        name: str,
        company: str,
        reply_snippet: str,
        campaign_id: str,
    ) -> dict:
        """Create a Task when a person exists, otherwise create a Lead."""
        person = self.find_person_by_email(email)
        snippet = (reply_snippet or "").strip().replace("\n", " ")
        snippet = snippet[:700]

        if person:
            task = self._run_sf_create(
                "Task",
                {
                    "Subject": f"Interested Reply - {name or email}",
                    "WhoId": person.get("Id", ""),
                    "Status": "Not Started",
                    "Priority": "Normal",
                    "ActivityDate": date.today().isoformat(),
                    "Description": (
                        f"Campaign: {campaign_id}\n"
                        f"Company: {company}\n"
                        f"Email: {email}\n"
                        f"Reply: {snippet}"
                    ),
                },
            )
            return {
                "action": "task_created",
                "record_id": task.get("id") or task.get("Id"),
                "matched_type": person.get("_sf_type"),
                "matched_id": person.get("Id"),
            }

        last_name = name.strip().split()[-1] if name and name.strip() else "Interested Reply"
        lead = self._run_sf_create(
            "Lead",
            {
                "LastName": last_name,
                "Company": company or "Unknown",
                "Email": email,
                "LeadSource": OUTBOUND_LEAD_SOURCE,
                "Lead_Source_Detail__c": OUTBOUND_LEAD_SOURCE_DETAIL,
                "Description": (
                    f"Campaign: {campaign_id}\n"
                    f"Name: {name}\n"
                    f"Reply: {snippet}"
                ),
            },
        )
        return {
            "action": "lead_created",
            "record_id": lead.get("id") or lead.get("Id"),
            "matched_type": None,
            "matched_id": None,
        }

    def _find_existing_task(self, who_id: str, subject: str) -> dict | None:
        if not who_id:
            return None
        safe_subject = _escape_sf_value(subject)
        records = self._run_sf_query(
            "SELECT Id, Subject, WhoId, Description FROM Task "
            f"WHERE WhoId = '{who_id}' "
            f"AND Subject = '{safe_subject}' "
            "LIMIT 1"
        )
        return records[0] if records else None

    def record_campaign_enrollment(
        self,
        *,
        email: str,
        name: str,
        company: str,
        internal_reference: str,
        smartlead_campaign_id: str,
        sequence_family: str,
    ) -> dict:
        """
        Route every Smartlead-enrolled contact into Salesforce.
        Existing Contact/Lead -> create one deduped Task per campaign.
        No existing person -> create a Lead.
        """
        person = self.find_person_by_email(email)
        campaign_marker = f"Smartlead campaign: {smartlead_campaign_id}"
        subject = f"Smartlead Enrollment - {sequence_family} - {smartlead_campaign_id}"

        if person:
            existing = self._find_existing_task(person.get("Id", ""), subject)
            if existing:
                return {
                    "action": "task_exists",
                    "record_id": existing.get("Id"),
                    "matched_type": person.get("_sf_type"),
                    "matched_id": person.get("Id"),
                }

            task = self._run_sf_create(
                "Task",
                {
                    "Subject": subject,
                    "WhoId": person.get("Id", ""),
                    "Status": "Not Started",
                    "Priority": "Normal",
                    "ActivityDate": date.today().isoformat(),
                    "Description": (
                        f"Campaign family: {sequence_family}\n"
                        f"{campaign_marker}\n"
                        f"Internal assignment reference: {internal_reference}\n"
                        f"Company: {company}\n"
                        f"Email: {email}\n"
                        "Created automatically from Smartlead enrollment."
                    ),
                },
            )
            return {
                "action": "task_created",
                "record_id": task.get("id") or task.get("Id"),
                "matched_type": person.get("_sf_type"),
                "matched_id": person.get("Id"),
            }

        last_name = name.strip().split()[-1] if name and name.strip() else "Smartlead Enrollment"
        lead = self._run_sf_create(
            "Lead",
            {
                "LastName": last_name,
                "Company": company or "Unknown",
                "Email": email,
                "LeadSource": OUTBOUND_LEAD_SOURCE,
                "Lead_Source_Detail__c": OUTBOUND_LEAD_SOURCE_DETAIL,
                "Description": (
                    f"Campaign family: {sequence_family}\n"
                    f"{campaign_marker}\n"
                    f"Internal assignment reference: {internal_reference}\n"
                    "Created automatically from Smartlead enrollment."
                ),
            },
        )
        return {
            "action": "lead_created",
            "record_id": lead.get("id") or lead.get("Id"),
            "matched_type": None,
            "matched_id": None,
        }


def sync_crm_suppressions() -> None:
    """
    Pull customer + open-opp domains from Salesforce and upsert into the suppressions table.
    Replaces all existing salesforce_cli rows on each run (clean sync).
    """
    client = CRMClient()

    print("[crm_client] Fetching customer domains from Salesforce...")
    customer_domains = client.get_customer_domains()
    print(f"[crm_client] {len(customer_domains)} customer domains")

    print("[crm_client] Fetching open-opp domains from Salesforce...")
    open_opp_domains = client.get_open_opp_domains()
    print(f"[crm_client] {len(open_opp_domains)} open-opp domains")

    with db_conn() as conn:
        with conn.cursor() as cur:
            # Clean sync — delete existing SF rows then re-insert
            cur.execute("DELETE FROM suppressions WHERE source = 'salesforce_cli'")

            for domain in customer_domains:
                cur.execute(
                    "INSERT INTO suppressions (domain, reason, source) VALUES (%s, %s, %s)",
                    (domain, "customer", "salesforce_cli"),
                )

            for domain in open_opp_domains:
                cur.execute(
                    "INSERT INTO suppressions (domain, reason, source) VALUES (%s, %s, %s)",
                    (domain, "open_opp", "salesforce_cli"),
                )

    total = len(customer_domains) + len(open_opp_domains)
    print(
        f"[crm_client] Sync complete. "
        f"{len(customer_domains)} customer + {len(open_opp_domains)} open-opp = {total} suppressions written."
    )
