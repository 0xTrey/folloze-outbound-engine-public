# Folloze Outbound Engine

Sanitized reference implementation for a signal-driven B2B outbound engine.

This public version keeps the system architecture, connector boundaries, job
orchestration, scoring logic, signal-memory model, and safety controls. It does
not include private production data, prospect lists, local logs, customer
exports, sender-domain cutover artifacts, live credentials, Folloze positioning,
persona guidance, message angles, proof, CTAs, sender voice, or source material.

## Private Messaging Configuration

Messaging is deliberately separated from the public engine. Authorized
collaborators should start with the [private Folloze messaging package](https://docs.google.com/document/d/1nIO_6O2odDh2ZHbRjVkVAk4H4q5JRu6ZUp5N6iapnA8/edit), which requires access.

Export the approved runtime inputs to local files that are never committed:

```text
private/messaging-context.md
private/proof-catalog.json
```

Then set `FOLLOZE_MESSAGING_CONTEXT_PATH` and
`FOLLOZE_PROOF_CATALOG_PATH`. Copy generation fails closed when the private
messaging context is missing. Destination URLs are also runtime-only settings.

## What It Does

The engine turns account/contact sourcing and first-party engagement events into
bounded outbound payloads:

```text
accounts + contacts
  -> research + scoring
  -> approved personalization payloads
  -> outbound enrollment
  -> reply and deliverability telemetry
  -> engagement events
  -> contact/account signal state
  -> structured outbound briefs for the next touch
```

The core design principle is interest memory rather than static lead scoring.
Every event is normalized, decayed by freshness, resolved to a person/account,
and converted into a structured brief before any LLM writes copy.

## Major Components

| Path | Purpose |
|---|---|
| `main.py` | CLI entrypoint and job dispatcher |
| `runtime_config.py` | environment loading, logging, job tracking, pause handling, notification wrapper |
| `connectors/` | API clients for CRM, outbound transport, enrichment, and research systems |
| `jobs/` | pipeline jobs for sourcing, enrichment, payload generation, sending, telemetry, and signal state |
| `db/` | schema and migration runner |
| `personalization/` | prompt routing, payload validation, case-study and objection selection |
| `routing/` | persona and sequence-family assignment |
| `scoring/` | account/contact fit and signal scoring |
| `reporting/` | scorecard and reporting helpers |
| `docs/engine-architecture-brief.html` | visual architecture brief |

## Local Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --help
```

This repo expects credentials to come from environment variables or macOS
Keychain. Do not commit real `.env` files.

## Configuration

| Env var | Purpose |
|---|---|
| `FOLLOZE_DATABASE_URL` | Postgres connection string |
| `FOLLOZE_SMARTLEAD_API_KEY` | Outbound transport API key |
| `FOLLOZE_APOLLO_API_KEY` | Account/contact enrichment key |
| `FOLLOZE_PERPLEXITY_API_KEY` | Research enrichment key |
| `FOLLOZE_BRAVE_API_KEY` | Search fallback key |
| `FOLLOZE_X_BEARER_TOKEN` | Optional social research token |
| `FOLLOZE_ALERT_CHANNEL` | Optional alert destination |
| `FOLLOZE_REPORT_TO` | Optional report recipient |
| `FOLLOZE_BOARD_INVITED_BY` | Optional identity parameter for board URLs |

## Safe Commands

Dry-run these first:

```bash
python main.py sync_accounts --dry-run --pages 1
python main.py reveal_emails --dry-run --batch 5
python main.py enrich_research --dry-run --batch 3
python main.py build_payloads --dry-run --batch 3
python main.py assign_push --dry-run --batch 3
python main.py engine_watchdog --dry-run
```

## Signal Loop

```text
raw engagement event
  -> normalized signal_event
  -> asset/topic resolution
  -> contact_signal_state + account_signal_state
  -> outbound_brief
  -> generated copy
  -> send outcome
  -> updated memory
```

The engine intentionally gates sends. A follow-up should not launch unless there
is recent first-party activity, enough evidence quality, a resolved message
angle, and no suppression/fatigue block.

## Public Repo Notes

This repository is sanitized for public sharing:

- no committed credentials
- no local runtime logs
- no prospect seed lists
- no customer CSV exports
- no production database dumps
- no sender-domain cutover artifacts
- no messaging framework or reference sources
- no customer proof catalog
- no hard-coded campaign destination URLs
- no private local git history from the source repo

If you adapt it for production, run your own secret scan and remove any company
or customer-specific data before publishing.
