#!/usr/bin/env python3
"""
Folloze Outbound Engine — CLI entrypoint

Usage:
  python main.py sync_accounts    [--dry-run] [--pages N]
  python main.py reveal_emails    [--dry-run] [--batch N]
  python main.py enrich_research  [--dry-run] [--batch N] [--stale-days N] [--domain D] [--contact-id ID]
  python main.py build_payloads   [--dry-run] [--batch N]
  python main.py migrate          # apply pending DB migrations
  python main.py setup_campaigns  [--dry-run]
  python main.py assign_push      [--dry-run] [--batch N]
  python main.py deliverability   [--dry-run]
  python main.py scorecard
  python main.py kill_domain   --domain acme.com [--reason manual]
  python main.py kill_campaign --campaign ENT_REVMKTG_INTENT
  python main.py kill_contact  --email jane@acme.com
  python main.py re_engage       [--dry-run] [--batch N] [--cycle-days N]
  python main.py pull_events     [--dry-run]
  python main.py sync_contact_state [--dry-run] [--export PATH]
  python main.py build_cutover_package [--output-dir PATH] [--no-sync]
  python main.py patch_sequences [--dry-run]
  python main.py sync_crm_suppressions
  python main.py sync_crm_contacts  [--dry-run] [--batch N] [--contact-id ID]
  python main.py sync_smartlead_mailboxes [--dry-run] [--sync-campaigns] [--active-families-only]
  python main.py warmup_smartlead_inboxes [--dry-run] [--mailbox VALUE] [--warmup-per-day N] [--daily-rampup N] [--reply-rate N] [--max-email-per-day N] [--wait-minutes N] [--sync-mailboxes] [--sync-campaigns] [--active-families-only]
  python main.py ingest_salesforce_folloze_events [--dry-run] [--limit N] [--since-days N] [--reset-cursor]
  python main.py ingest_folloze_people_csv [--path PATH] [--dry-run] [--limit N] [--expected-board-id ID] [--expected-campaign NAME] [--max-report-age-hours N]
  python main.py import_folloze_content_catalog [--path PATH] [--overrides PATH] [--dry-run] [--limit N]
  python main.py derive_contact_signal_state [--dry-run] [--lookback-days N] [--contact-id ID]
  python main.py build_outbound_briefs [--dry-run] [--limit N] [--contact-id ID]
  python main.py engine_watchdog [--dry-run] [--auto-retry-safe] [--job NAME]
  python main.py daily_report      [--dry-run]

Recommended order for a full run:
  1. sync_accounts    — pull ICP accounts + contacts from Apollo, score + route
  2. reveal_emails    — bulk-reveal email + last_name + linkedin for Apollo contacts
  3. enrich_research  — collect all signals (Perplexity/Brave/X), store brief per contact
  4. build_payloads   — generate personalization via LLM (reads from contact_research)
  5. setup_campaigns  — create Smartlead campaigns + 2-step sequences (one-time per family)
  6. assign_push      — push approved contacts to Smartlead

Re-engagement cycle (runs every ~45 days after first send):
  8. re_engage        — rotate silent contacts to a new family, queue for build_payloads
  9. build_payloads   — regenerate payloads for re-engaged contacts
  10. assign_push     — push re-engaged cohort

Note: enrich_signals is kept for backward compatibility but is superseded by enrich_research.
"""
import argparse
import sys

from runtime_config import assert_repo_context, run_tracked_job, should_skip_for_pause


def main():
    parser = argparse.ArgumentParser(prog="outbound-engine")
    sub = parser.add_subparsers(dest="job", required=True)

    # sync_accounts
    p_sync = sub.add_parser("sync_accounts", help="Pull accounts + contacts from Apollo")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--pages", type=int, default=4)

    # reveal_emails
    p_reveal = sub.add_parser("reveal_emails", help="Bulk-reveal email + profile for Apollo contacts")
    p_reveal.add_argument("--dry-run", action="store_true")
    p_reveal.add_argument("--batch", type=int, default=100)

    # enrich_research
    p_research = sub.add_parser(
        "enrich_research",
        help="Collect all signals (Perplexity/Brave/X) and store a research brief per contact",
    )
    p_research.add_argument("--dry-run", action="store_true")
    p_research.add_argument("--batch", type=int, default=20)
    p_research.add_argument("--stale-days", type=int, default=7, dest="stale_days")
    p_research.add_argument("--domain", type=str, default=None, help="Enrich contacts at a single domain")
    p_research.add_argument("--contact-id", type=str, default=None, dest="contact_id", help="Enrich a single contact by UUID")


    # build_payloads
    p_build = sub.add_parser("build_payloads", help="Generate personalization payloads")
    p_build.add_argument("--dry-run", action="store_true")
    p_build.add_argument("--batch", type=int, default=50)

    # setup_campaigns
    p_setup = sub.add_parser("setup_campaigns", help="Create Smartlead campaigns + sequences (idempotent)")
    p_setup.add_argument("--dry-run", action="store_true")

    # assign_push
    p_push = sub.add_parser("assign_push", help="Push approved contacts to Smartlead (Phase 2)")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument("--batch", type=int, default=50)

    # deliverability
    p_del = sub.add_parser("deliverability", help="Sync unsubscribes and bounces from Smartlead → suppressions")
    p_del.add_argument("--dry-run", action="store_true")

    # scorecard
    sub.add_parser("scorecard", help="Print daily KPI report")

    # kill_domain
    p_kd = sub.add_parser("kill_domain", help="Suppress all contacts from a domain")
    p_kd.add_argument("--domain", required=True)
    p_kd.add_argument("--reason", default="manual")

    # kill_campaign
    p_kc = sub.add_parser("kill_campaign", help="Pause a sequence family")
    p_kc.add_argument("--campaign", required=True)

    # kill_contact
    p_ke = sub.add_parser("kill_contact", help="Suppress a contact by email")
    p_ke.add_argument("--email", required=True)
    p_ke.add_argument("--reason", default="manual")

    # re_engage
    p_reengage = sub.add_parser("re_engage", help="Queue silent contacts for 45-day re-engagement cycle")
    p_reengage.add_argument("--dry-run", action="store_true")
    p_reengage.add_argument("--batch", type=int, default=100)
    p_reengage.add_argument("--cycle-days", type=int, default=45, dest="cycle_days")

    # pull_events
    p_pull = sub.add_parser("pull_events", help="Poll Smartlead for replies and bounces, classify, notify")
    p_pull.add_argument("--dry-run", action="store_true")

    # sync_contact_state
    p_contact_state = sub.add_parser(
        "sync_contact_state",
        help="Sync a durable current-state snapshot for every contact and optionally export it",
    )
    p_contact_state.add_argument("--dry-run", action="store_true")
    p_contact_state.add_argument("--export", type=str, default=None, help="Write the derived contact state to CSV")

    # build_cutover_package
    p_cutover = sub.add_parser(
        "build_cutover_package",
        help="Export the live Smartlead + Neon package needed for a sender-domain transfer",
    )
    p_cutover.add_argument("--output-dir", type=str, default=None, help="Directory to write the package into")
    p_cutover.add_argument("--no-sync", action="store_true", help="Reuse an existing contact-state.csv in the output directory")

    # patch_sequences
    p_patch = sub.add_parser("patch_sequences", help="Update live campaign templates to add case study link placeholders")
    p_patch.add_argument("--dry-run", action="store_true")

    # sync_crm_suppressions
    sub.add_parser("sync_crm_suppressions", help="Pull Salesforce customers + open opps into suppressions table")

    # sync_crm_contacts
    p_sync_crm_contacts = sub.add_parser(
        "sync_crm_contacts",
        help="Create or update Salesforce person records for outbound-engine contacts",
    )
    p_sync_crm_contacts.add_argument("--dry-run", action="store_true")
    p_sync_crm_contacts.add_argument("--batch", type=int, default=1000)
    p_sync_crm_contacts.add_argument("--contact-id", type=str, default=None, dest="contact_id")

    # sync_smartlead_mailboxes
    p_sync_smartlead_mailboxes = sub.add_parser(
        "sync_smartlead_mailboxes",
        help="Sync Smartlead inbox state into the local mailboxes table and optionally reconcile campaign mailbox assignments",
    )
    p_sync_smartlead_mailboxes.add_argument("--dry-run", action="store_true")
    p_sync_smartlead_mailboxes.add_argument("--sync-campaigns", action="store_true", dest="sync_campaigns")
    p_sync_smartlead_mailboxes.add_argument("--active-families-only", action="store_true", dest="active_families_only")

    # warmup_smartlead_inboxes
    p_warmup_smartlead_inboxes = sub.add_parser(
        "warmup_smartlead_inboxes",
        help="Configure Smartlead inbox warmup settings and optionally sync mailbox/campaign state",
    )
    p_warmup_smartlead_inboxes.add_argument("--dry-run", action="store_true")
    p_warmup_smartlead_inboxes.add_argument(
        "--mailbox",
        action="append",
        dest="mailboxes",
        help="Mailbox email address or Smartlead email-account id to target; repeatable",
    )
    p_warmup_smartlead_inboxes.add_argument("--warmup-per-day", type=int, default=20, dest="warmup_per_day")
    p_warmup_smartlead_inboxes.add_argument("--daily-rampup", type=int, default=5, dest="daily_rampup")
    p_warmup_smartlead_inboxes.add_argument("--reply-rate", type=int, default=30, dest="reply_rate")
    p_warmup_smartlead_inboxes.add_argument("--max-email-per-day", type=int, default=30, dest="max_email_per_day")
    p_warmup_smartlead_inboxes.add_argument("--wait-minutes", type=int, default=20, dest="wait_minutes")
    p_warmup_smartlead_inboxes.add_argument("--sync-mailboxes", action="store_true", dest="sync_mailboxes")
    p_warmup_smartlead_inboxes.add_argument("--sync-campaigns", action="store_true", dest="sync_campaigns")
    p_warmup_smartlead_inboxes.add_argument("--active-families-only", action="store_true", dest="active_families_only")

    # ingest_salesforce_folloze_events
    p_ingest_sf_folloze = sub.add_parser(
        "ingest_salesforce_folloze_events",
        help="Ingest normalized Folloze Task events from Salesforce into signal_events",
    )
    p_ingest_sf_folloze.add_argument("--dry-run", action="store_true")
    p_ingest_sf_folloze.add_argument("--limit", type=int, default=500)
    p_ingest_sf_folloze.add_argument("--since-days", type=int, default=7, dest="since_days")
    p_ingest_sf_folloze.add_argument("--reset-cursor", action="store_true", dest="reset_cursor")

    # ingest_folloze_people_csv
    p_ingest_people_csv = sub.add_parser(
        "ingest_folloze_people_csv",
        help="Ingest a Folloze People Engagement CSV snapshot into signal_events",
    )
    p_ingest_people_csv.add_argument("--path", type=str, default=None)
    p_ingest_people_csv.add_argument("--dry-run", action="store_true")
    p_ingest_people_csv.add_argument("--limit", type=int, default=None)
    p_ingest_people_csv.add_argument("--expected-board-id", type=str, default=None, dest="expected_board_id")
    p_ingest_people_csv.add_argument("--expected-campaign", type=str, default=None, dest="expected_campaign")
    p_ingest_people_csv.add_argument("--max-report-age-hours", type=int, default=72, dest="max_report_age_hours")

    # import_folloze_content_catalog
    p_import_content_catalog = sub.add_parser(
        "import_folloze_content_catalog",
        help="Import the Folloze Content Engagement report into asset_catalog",
    )
    p_import_content_catalog.add_argument("--path", type=str, default=None)
    p_import_content_catalog.add_argument("--overrides", type=str, default=None)
    p_import_content_catalog.add_argument("--dry-run", action="store_true")
    p_import_content_catalog.add_argument("--limit", type=int, default=None)

    # derive_contact_signal_state
    p_derive_contact_signal_state = sub.add_parser(
        "derive_contact_signal_state",
        help="Derive prompt-ready contact_signal_state and account_signal_state from signal_events",
    )
    p_derive_contact_signal_state.add_argument("--dry-run", action="store_true")
    p_derive_contact_signal_state.add_argument("--lookback-days", type=int, default=60, dest="lookback_days")
    p_derive_contact_signal_state.add_argument("--contact-id", type=str, default=None, dest="contact_id")

    # build_outbound_briefs
    p_build_outbound_briefs = sub.add_parser(
        "build_outbound_briefs",
        help="Materialize structured outbound_briefs from contact_signal_state and research evidence",
    )
    p_build_outbound_briefs.add_argument("--dry-run", action="store_true")
    p_build_outbound_briefs.add_argument("--limit", type=int, default=100)
    p_build_outbound_briefs.add_argument("--contact-id", type=str, default=None, dest="contact_id")

    # engine_watchdog
    p_engine_watchdog = sub.add_parser(
        "engine_watchdog",
        help="Run the engine-wide scheduler watchdog and safe-retry canary",
    )
    p_engine_watchdog.add_argument("--dry-run", action="store_true")
    p_engine_watchdog.add_argument("--auto-retry-safe", action="store_true", dest="auto_retry_safe")
    p_engine_watchdog.add_argument("--job", type=str, default=None, dest="watchdog_job")

    # migrate
    sub.add_parser("migrate", help="Apply pending DB migrations")

    # daily_report
    p_daily = sub.add_parser("daily_report", help="Post daily outbound activity summary to Mason Discord")
    p_daily.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    assert_repo_context()

    skip_job, skip_message = should_skip_for_pause(args.job)
    if skip_job:
        print(skip_message)
        return

    if args.job == "sync_accounts":
        from jobs.sync_accounts import run
        run_tracked_job("sync_accounts", run, dry_run=args.dry_run, pages=args.pages)

    elif args.job == "reveal_emails":
        from jobs.reveal_emails import run
        run_tracked_job("reveal_emails", run, dry_run=args.dry_run, batch=args.batch)

    elif args.job == "enrich_research":
        from jobs.enrich_research import run
        run_tracked_job(
            "enrich_research",
            run,
            dry_run=args.dry_run,
            batch_size=args.batch,
            stale_days=args.stale_days,
            domain_filter=args.domain,
            contact_id_filter=args.contact_id,
        )

    elif args.job == "build_payloads":
        from jobs.build_payloads import run
        run_tracked_job("build_payloads", run, dry_run=args.dry_run, batch_size=args.batch)

    elif args.job == "setup_campaigns":
        from jobs.setup_campaigns import run
        run_tracked_job("setup_campaigns", run, dry_run=args.dry_run)

    elif args.job == "assign_push":
        from jobs.assign_push import run
        run_tracked_job("assign_push", run, dry_run=args.dry_run, batch=args.batch)

    elif args.job == "deliverability":
        from jobs.deliverability import run
        run_tracked_job("deliverability", run, dry_run=args.dry_run)

    elif args.job == "scorecard":
        from reporting.scorecard import run
        run_tracked_job("scorecard", run)

    elif args.job == "kill_domain":
        from ops.kill_switches import pause_domain
        run_tracked_job("kill_domain", pause_domain, args.domain, reason=args.reason)

    elif args.job == "kill_campaign":
        from ops.kill_switches import pause_campaign
        run_tracked_job("kill_campaign", pause_campaign, args.campaign)

    elif args.job == "kill_contact":
        from ops.kill_switches import suppress_contact
        run_tracked_job("kill_contact", suppress_contact, args.email, reason=args.reason)

    elif args.job == "re_engage":
        from jobs.re_engage import run
        run_tracked_job(
            "re_engage",
            run,
            dry_run=args.dry_run,
            batch_size=args.batch,
            cycle_days=args.cycle_days,
        )

    elif args.job == "pull_events":
        from jobs.pull_events import run
        run_tracked_job("pull_events", run, dry_run=args.dry_run)

    elif args.job == "sync_contact_state":
        from jobs.sync_contact_state import run
        run_tracked_job("sync_contact_state", run, dry_run=args.dry_run, export_path=args.export)

    elif args.job == "build_cutover_package":
        from jobs.build_cutover_package import run
        run_tracked_job("build_cutover_package", run, output_dir=args.output_dir, sync_state=not args.no_sync)

    elif args.job == "patch_sequences":
        from jobs.patch_sequences import run
        run_tracked_job("patch_sequences", run, dry_run=args.dry_run)

    elif args.job == "sync_crm_suppressions":
        from connectors.crm_client import sync_crm_suppressions
        run_tracked_job("sync_crm_suppressions", sync_crm_suppressions)

    elif args.job == "sync_crm_contacts":
        from jobs.sync_crm_contacts import run
        run_tracked_job(
            "sync_crm_contacts",
            run,
            dry_run=args.dry_run,
            batch_size=args.batch,
            contact_id_filter=args.contact_id,
        )

    elif args.job == "sync_smartlead_mailboxes":
        from jobs.sync_smartlead_mailboxes import run
        run_tracked_job(
            "sync_smartlead_mailboxes",
            run,
            dry_run=args.dry_run,
            sync_campaigns=args.sync_campaigns,
            active_families_only=args.active_families_only,
        )

    elif args.job == "warmup_smartlead_inboxes":
        from jobs.warmup_smartlead_inboxes import run
        run_tracked_job(
            "warmup_smartlead_inboxes",
            run,
            dry_run=args.dry_run,
            mailbox_filters=args.mailboxes,
            warmup_per_day=args.warmup_per_day,
            daily_rampup=args.daily_rampup,
            reply_rate=args.reply_rate,
            max_email_per_day=args.max_email_per_day,
            wait_minutes=args.wait_minutes,
            sync_mailboxes=args.sync_mailboxes,
            sync_campaigns=args.sync_campaigns,
            active_families_only=args.active_families_only,
        )

    elif args.job == "ingest_salesforce_folloze_events":
        from jobs.ingest_salesforce_folloze_events import run
        run_tracked_job(
            "ingest_salesforce_folloze_events",
            run,
            dry_run=args.dry_run,
            limit=args.limit,
            since_days=args.since_days,
            reset_cursor=args.reset_cursor,
        )

    elif args.job == "ingest_folloze_people_csv":
        from jobs.ingest_folloze_people_csv import run
        run_tracked_job(
            "ingest_folloze_people_csv",
            run,
            path=args.path,
            dry_run=args.dry_run,
            limit=args.limit,
            expected_board_id=args.expected_board_id,
            expected_campaign=args.expected_campaign,
            max_report_age_hours=args.max_report_age_hours,
        )

    elif args.job == "import_folloze_content_catalog":
        from jobs.import_folloze_content_catalog import run
        run_tracked_job(
            "import_folloze_content_catalog",
            run,
            path=args.path,
            overrides_path=args.overrides,
            dry_run=args.dry_run,
            limit=args.limit,
        )

    elif args.job == "derive_contact_signal_state":
        from jobs.derive_contact_signal_state import run
        run_tracked_job(
            "derive_contact_signal_state",
            run,
            dry_run=args.dry_run,
            lookback_days=args.lookback_days,
            contact_id_filter=args.contact_id,
        )

    elif args.job == "build_outbound_briefs":
        from jobs.build_outbound_briefs import run
        run_tracked_job(
            "build_outbound_briefs",
            run,
            dry_run=args.dry_run,
            limit=args.limit,
            contact_id_filter=args.contact_id,
        )

    elif args.job == "engine_watchdog":
        from jobs.engine_watchdog import run
        run_tracked_job(
            "engine_watchdog",
            run,
            dry_run=args.dry_run,
            auto_retry_safe=args.auto_retry_safe,
            target_job=args.watchdog_job,
        )

    elif args.job == "migrate":
        from db.migrations import apply_pending
        from db.connection import db_conn

        def _run_migrate():
            with db_conn() as conn:
                return apply_pending(conn)

        run_tracked_job("migrate", _run_migrate)

    elif args.job == "daily_report":
        from jobs.daily_report import run
        run_tracked_job("daily_report", run, dry_run=args.dry_run)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
