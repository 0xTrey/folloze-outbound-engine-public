"""
Job: setup_campaigns
Idempotent Smartlead campaign provisioning for all 6 sequence families.

Creates campaigns with 2-step sequences and assigns warmed mailboxes.
Skips families that already have a smartlead_campaign_id in Neon.
Updates sequence_families.smartlead_campaign_id after creation.

Sequence structure (per the 2-email methodology):
  Step 1 (day 0):   New thread. Subject + full body with personalisation variables.
  Step 2 (day 3):   Threaded reply. Different angle, genuine close.

45-day re-engagement is handled by the re_engage job — not by adding more steps here.

Usage:
    python main.py setup_campaigns --dry-run    # print what would be created
    python main.py setup_campaigns              # create in Smartlead + update Neon

Prerequisites:
  1. SMARTLEAD_API_KEY in environment (via ~/.zshrc Keychain export)
  2. sequence_families table populated with all 6 families
  3. Email accounts connected and warmed in Smartlead
"""
from __future__ import annotations

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn

# ---------------------------------------------------------------------------
# Sequence step templates
# All 6 campaigns use the same body structure — custom_fields vary per contact.
# Smartlead merges {{variable}} placeholders when the lead is added.
# ---------------------------------------------------------------------------

SIGNATURE = """\
--
{{sender_name}}
{{sender_title}}
{{sender_company}}
{{unsubscribe}}"""

STEP_1_BODY = f"""\
{{{{ai_opener}}}}

{{{{ai_why_now}}}}

{{{{ai_proof_point}}}}

{{{{ai_cta}}}}{{{{ai_t1_case_study_link}}}}

{SIGNATURE}"""

STEP_2_BODY = """\
{{ai_t2_body}}{{ai_t2_case_study_link}}

{{unsubscribe}}"""

# T3 is used by GTME_VIBE_MKT only (3-touch sequence, day 7).
STEP_3_BODY = """\
{{ai_t3_body}}{{ai_t3_case_study_link}}

{{unsubscribe}}"""

# Families that use a 3-step sequence instead of the standard 2-step.
THREE_STEP_FAMILIES = {"GTME_VIBE_MKT"}


def _mailbox_label(account: dict) -> str:
    return (
        account.get("from_email")
        or account.get("email")
        or account.get("username")
        or str(account.get("id") or "unknown")
    )


def _mailbox_status(account: dict) -> str:
    warmup_status = str(((account.get("warmup_details") or {}).get("status") or "")).strip().upper()
    if warmup_status:
        return warmup_status
    if account.get("is_imap_success") and account.get("is_smtp_success"):
        return "CONNECTED"
    return "UNHEALTHY"


def _is_assignable_email_account(account: dict) -> bool:
    status = _mailbox_status(account)
    if status in {"ACTIVE", "WARMING", "CONNECTED"}:
        return True
    if status in {"PAUSED", "STOPPED", "DISABLED", "FAILED", "ERROR", "UNHEALTHY"}:
        return False
    return bool(account.get("is_imap_success") and account.get("is_smtp_success"))


def _select_assignable_email_accounts(email_accounts: list[dict]) -> list[dict]:
    return [account for account in email_accounts if _is_assignable_email_account(account)]


def _sync_campaign_email_accounts(
    smartlead: SmartleadClient,
    campaign_id: str,
    desired_accounts: list[dict],
    *,
    dry_run: bool = False,
) -> dict:
    desired_ids = sorted(
        int(account["id"])
        for account in desired_accounts
        if account.get("id") is not None
    )
    desired_labels = [_mailbox_label(account) for account in desired_accounts]

    current_accounts = smartlead.get_campaign_email_accounts(campaign_id)
    current_ids = sorted(
        int(account["id"])
        for account in current_accounts
        if account.get("id") is not None
    )
    current_labels = [_mailbox_label(account) for account in current_accounts]

    if current_ids == desired_ids:
        return {
            "status": "unchanged",
            "current_ids": current_ids,
            "current_labels": current_labels,
            "desired_ids": desired_ids,
            "desired_labels": desired_labels,
        }

    if dry_run:
        return {
            "status": "would_update",
            "current_ids": current_ids,
            "current_labels": current_labels,
            "desired_ids": desired_ids,
            "desired_labels": desired_labels,
        }

    smartlead.assign_email_accounts_to_campaign(campaign_id, desired_ids)
    return {
        "status": "updated",
        "current_ids": current_ids,
        "current_labels": current_labels,
        "desired_ids": desired_ids,
        "desired_labels": desired_labels,
    }


def _build_sequence_steps(subject_placeholder: str, family_name: str = "") -> list[dict]:
    """
    Build the sequence steps for a campaign family.

    Standard (2-step):
      Step 1 (day 0):  New thread. ai_subject + full personalisation body.
      Step 2 (day 3):  Threaded reply. Different angle, genuine close.

    3-step (GTME_VIBE_MKT):
      Step 1 (day 0):  Same as above.
      Step 2 (day 3):  Claude meta-play — transparent about the build stack.
      Step 3 (day 7):  Genuine close / binary out. (delay=4 from step 2)

    45-day re-engagement runs as a separate campaign cycle via the re_engage job.
    """
    steps = [
        {
            "seq_number": 1,
            "subject": subject_placeholder,
            "email_body": STEP_1_BODY,
            "seq_delay_details": {"delay_in_days": 0},
        },
        {
            "seq_number": 2,
            "subject": "",  # threaded — Smartlead sends as "Re: [T1 subject]"
            "email_body": STEP_2_BODY,
            "seq_delay_details": {"delay_in_days": 3},
        },
    ]

    if family_name in THREE_STEP_FAMILIES:
        steps.append({
            "seq_number": 3,
            "subject": "",  # threaded
            "email_body": STEP_3_BODY,
            "seq_delay_details": {"delay_in_days": 4},  # day 3 + 4 = day 7 from T1
        })

    return steps


def _load_families(conn) -> list[dict]:
    """Load all active sequence families from Neon."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT family_id, family_name, smartlead_campaign_id
            FROM sequence_families
            WHERE is_active = TRUE
            ORDER BY family_name
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _update_campaign_id(conn, family_name: str, campaign_id: str):
    """Write the Smartlead campaign ID back to sequence_families."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sequence_families
            SET smartlead_campaign_id = %s
            WHERE family_name = %s
        """, (campaign_id, family_name))
    conn.commit()


def run(dry_run: bool = False):
    print(f"[setup_campaigns] Starting. dry_run={dry_run}")

    with db_conn() as conn:
        families = _load_families(conn)

    if not families:
        print("[setup_campaigns] No active sequence families found in sequence_families table.")
        print("  Seed the table first: INSERT INTO sequence_families (family_name, segment, persona_type, trigger_type, is_active) VALUES (...)")
        return

    print(f"[setup_campaigns] {len(families)} active families loaded.")

    # Build a map of existing Smartlead campaign names → IDs
    smartlead = SmartleadClient()
    existing_campaigns = smartlead.list_campaigns()
    existing_by_name = {c["name"]: c["id"] for c in existing_campaigns}
    print(f"[setup_campaigns] {len(existing_campaigns)} existing Smartlead campaigns found.")

    # Get all currently usable email accounts for mailbox assignment.
    # Smartlead sometimes returns no warmup_details for fresh-but-connected inboxes,
    # so we also accept accounts with healthy IMAP + SMTP connectivity.
    email_accounts = smartlead.list_email_accounts()
    assignable_accounts = _select_assignable_email_accounts(email_accounts)
    print(
        f"[setup_campaigns] {len(assignable_accounts)} assignable email accounts available"
        f" out of {len(email_accounts)} connected."
    )
    for account in assignable_accounts:
        print(f"    mailbox {_mailbox_label(account)} status={_mailbox_status(account)} id={account.get('id')}")

    created = 0
    skipped = 0
    mailbox_synced = 0

    for family in families:
        name = family["family_name"]
        existing_campaign_id = family.get("smartlead_campaign_id")

        if existing_campaign_id:
            print(f"  [sync] {name} — existing campaign_id={existing_campaign_id}")
            if assignable_accounts:
                mailbox_result = _sync_campaign_email_accounts(
                    smartlead,
                    str(existing_campaign_id),
                    assignable_accounts,
                    dry_run=dry_run,
                )
                print(
                    f"    mailbox sync: {mailbox_result['status']}"
                    f" current={mailbox_result['current_labels']}"
                    f" desired={mailbox_result['desired_labels']}"
                )
                if mailbox_result["status"] in {"updated", "would_update"}:
                    mailbox_synced += 1
            else:
                print("    WARNING: no assignable email accounts available to sync")
            skipped += 1
            continue

        # Check if a Smartlead campaign with this name already exists
        if name in existing_by_name:
            sl_id = str(existing_by_name[name])
            print(f"  [found] {name} exists in Smartlead (id={sl_id}) — updating Neon only")
            if not dry_run:
                with db_conn() as conn:
                    _update_campaign_id(conn, name, sl_id)
            else:
                print(f"    [DRY RUN] would UPDATE sequence_families SET smartlead_campaign_id='{sl_id}' WHERE family_name='{name}'")
            if assignable_accounts:
                mailbox_result = _sync_campaign_email_accounts(
                    smartlead,
                    sl_id,
                    assignable_accounts,
                    dry_run=dry_run,
                )
                print(
                    f"    mailbox sync: {mailbox_result['status']}"
                    f" current={mailbox_result['current_labels']}"
                    f" desired={mailbox_result['desired_labels']}"
                )
                if mailbox_result["status"] in {"updated", "would_update"}:
                    mailbox_synced += 1
            else:
                print("    WARNING: no assignable email accounts available to sync")
            skipped += 1
            continue

        print(f"  [create] {name}")

        if dry_run:
            print(f"    [DRY RUN] POST /campaigns/create name='{name}'")
            print(f"    [DRY RUN] POST /campaigns/{{id}}/settings")
            print(f"    [DRY RUN] POST /campaigns/{{id}}/sequences — 2 steps")
            if assignable_accounts:
                print(f"    [DRY RUN] POST /campaigns/{{id}}/email-accounts — {len(assignable_accounts)} mailboxes")
            else:
                print(f"    [DRY RUN] WARNING: no assignable email accounts to assign")
            print(f"    [DRY RUN] POST /campaigns/{{id}}/schedule — Mon-Fri 8am-5pm CT")
            print(f"    [DRY RUN] POST /campaigns/{{id}}/status START")

            # Print template preview
            steps = _build_sequence_steps("{{ai_subject}}", family_name=name)
            for step in steps:
                subj = f"subject='{step['subject']}'" if step["subject"] else "subject='' (threaded reply)"
                print(f"    Step {step['seq_number']} | day+{step['seq_delay_details']['delay_in_days']} | {subj}")
                body_preview = step["email_body"][:80].replace("\n", " ↵ ")
                print(f"      body preview: {body_preview}...")
            created += 1
            continue

        # 1. Create campaign
        campaign_result = smartlead.create_campaign(name=name)
        campaign_id = str(campaign_result.get("id") or "")
        if not campaign_id:
            print(f"    [ERROR] Campaign creation returned no ID: {campaign_result}")
            continue
        print(f"    Created campaign id={campaign_id}")

        # 1b. Apply behavior settings
        smartlead.update_campaign_settings(campaign_id, {
            "stop_lead_settings": "REPLY_TO_AN_EMAIL",
            "follow_up_percentage": 100,
            "enable_ai_esp_matching": False,
        })
        print(f"    Settings applied")

        # 2. Create 5-step sequence
        steps = _build_sequence_steps("{{ai_subject}}", family_name=name)
        seq_result = smartlead.create_sequence(campaign_id, steps)
        print(f"    Sequence created: {seq_result}")

        # 3. Assign email accounts (mailboxes) for inbox rotation
        if assignable_accounts:
            mailbox_result = _sync_campaign_email_accounts(
                smartlead,
                campaign_id,
                assignable_accounts,
                dry_run=dry_run,
            )
            print(
                f"    Mailbox sync: {mailbox_result['status']}"
                f" current={mailbox_result['current_labels']}"
                f" desired={mailbox_result['desired_labels']}"
            )
            if mailbox_result["status"] in {"updated", "would_update"}:
                mailbox_synced += 1
        else:
            print(f"    WARNING: no assignable email accounts to assign to {name}")

        # 4. Set schedule (required before activation)
        smartlead.set_campaign_schedule(campaign_id)
        print(f"    Schedule set: Mon-Fri 8am-5pm CT, 30min between sends, 50 new leads/day")

        # 5. Activate campaign
        smartlead.start_campaign(campaign_id)
        print(f"    Campaign activated (ACTIVE)")

        # 6. Write campaign ID back to Neon
        with db_conn() as conn:
            _update_campaign_id(conn, name, campaign_id)
        print(f"    Neon updated: sequence_families.smartlead_campaign_id = '{campaign_id}'")

        created += 1

    print(f"[setup_campaigns] Done. created={created} skipped={skipped} mailbox_synced={mailbox_synced}")
