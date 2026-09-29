"""
Job: build_outbound_briefs

Materialize structured outbound briefs from signal state, asset semantics, and
available research evidence.

This is the contract layer between "we observed engagement" and "the model can
write from a controlled, auditable brief."

Usage:
    python main.py build_outbound_briefs
    python main.py build_outbound_briefs --dry-run
    python main.py build_outbound_briefs --limit 25
    python main.py build_outbound_briefs --contact-id <uuid>
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg2.extras as psycopg2_extras
except ImportError:
    psycopg2_extras = None

from db.connection import db_conn, try_advisory_xact_lock
from runtime_config import configure_logging

logger = logging.getLogger(__name__)

BUILD_OUTBOUND_BRIEFS_LOCK = "build_outbound_briefs"
FIRST_PARTY_FRESHNESS_DAYS = 14

TRIGGER_PRIORITY = {
    "HIRING": 0,
    "FUNDING": 1,
    "PRODUCT": 2,
    "EXEC": 3,
    "EVENT": 4,
    "PARTNER": 5,
    "WEB-CONTACT": 6,
    "WEB": 7,
    "SNAPSHOT": 8,
}

THEME_LABELS = {
    "persona_personalization": "persona personalization",
    "campaign_automation": "campaign automation",
    "nurture_orchestration": "nurture orchestration",
    "abm_personalization": "ABM personalization",
}

PLAY_LIBRARY: dict[str, dict[str, Any]] = {
    "intent_surge_plus_engaged_content_path": {
        "topic_focus": ["active_interest", "engaged_content_path"],
        "proof_points": [
            "Folloze turns fresh engagement into a board plus seller-ready follow-up path.",
            "Teams can move from a content moment to a tailored next step instead of a generic nurture stream.",
            "Board engagement gives sales a cleaner reason to follow up than intent data alone.",
        ],
        "cta_style": "board_first",
    },
    "new_demand_gen_leader": {
        "topic_focus": ["pipeline_efficiency", "campaign_execution"],
        "proof_points": [
            "Folloze helps demand gen teams launch tailored programs without multiplying operational drag.",
            "Shared board templates and modular content let teams move faster across personas and plays.",
        ],
        "cta_style": "operator_first",
    },
    "new_abm_leader": {
        "topic_focus": ["buying_group_activation", "abm_personalization"],
        "proof_points": [
            "Folloze helps ABM teams scale 1:few and 1:1 experiences without a content fire drill.",
            "Board-level engagement gives account teams better signal on which stakeholders are warming up.",
        ],
        "cta_style": "abm_first",
    },
    "new_marketing_ops_or_revops_leader": {
        "topic_focus": ["signal_quality", "workflow_governance"],
        "proof_points": [
            "Folloze fits into the existing GTM stack instead of forcing a rip-and-replace motion.",
            "Signals, boards, and follow-up can be governed in one workflow instead of split across disconnected tools.",
        ],
        "cta_style": "operator_first",
    },
    "hiring_surge_in_demand_gen_or_abm": {
        "topic_focus": ["team_capacity", "execution_leverage"],
        "proof_points": [
            "Folloze helps lean teams get more leverage from existing content and outbound motion.",
            "Reusable boards and persona pathways reduce the manual work required to personalize at scale.",
        ],
        "cta_style": "operator_first",
    },
    "mutiny_present": {
        "topic_focus": ["website_personalization", "journey_orchestration"],
        "proof_points": [
            "Folloze picks up where page-level personalization leaves off by carrying the journey into follow-up.",
            "Sales can work from the same engagement context the web team already sees.",
        ],
        "cta_style": "competitive_first",
    },
    "pathfactory_present": {
        "topic_focus": ["content_intelligence", "sales_activation"],
        "proof_points": [
            "Folloze makes rich content engagement data more actionable for follow-up and buying-group activation.",
            "Teams can route buyers into a clearer next step instead of leaving engagement trapped inside content analytics.",
        ],
        "cta_style": "competitive_first",
    },
    "userled_or_scalable_abm_platform_present": {
        "topic_focus": ["scalable_abm", "pipeline_visibility"],
        "proof_points": [
            "Folloze deepens account experiences with a stronger connection between engagement and pipeline action.",
            "Programs can keep the personalized experience while giving sellers a clearer path to follow up.",
        ],
        "cta_style": "competitive_first",
    },
    "ai_forward_marketing_team": {
        "topic_focus": ["ai_signal_processing", "workflow_automation"],
        "proof_points": [
            "Folloze helps teams use AI for signal interpretation and next-best-action execution, not just content drafting.",
            "Automation can stay governed and observable instead of becoming another black-box experiment.",
        ],
        "cta_style": "technical_first",
    },
}

PERSONA_DEFAULT_PLAYS = {
    "revmktg_vp": "new_demand_gen_leader",
    "abm_leader": "new_abm_leader",
    "revops": "new_marketing_ops_or_revops_leader",
    "marketing_ops": "new_marketing_ops_or_revops_leader",
}

TECH_TRIGGER_PLAYS = {
    "mutiny": "mutiny_present",
    "pathfactory": "pathfactory_present",
    "userled": "userled_or_scalable_abm_platform_present",
}

COMPETITOR_TOOLS = {"6sense", "demandbase"}
MAP_TOOLS = {"marketo", "eloqua"}


def _dedupe_preserve_order(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _trim_text(value: Any, max_len: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _humanize_token(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("_", " ").replace("-", " ").strip()
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in text.split())


def _theme_label(theme: str | None) -> str:
    if not theme:
        return ""
    return THEME_LABELS.get(str(theme), _humanize_token(str(theme))).strip()


def _safe_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_tech_names(apollo_raw: Any) -> list[str]:
    raw = _safe_dict(apollo_raw)
    tech = raw.get("current_technologies") or []
    names: list[str] = []
    for item in tech:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = str(item)
        if name:
            names.append(str(name))
    return names


def _select_play_id(row: dict[str, Any]) -> str | None:
    if row.get("recommended_play_id"):
        return str(row["recommended_play_id"])
    if row.get("account_recommended_play_id"):
        return str(row["account_recommended_play_id"])

    tech_names = [name.lower() for name in _extract_tech_names(row.get("apollo_raw"))]
    for name in tech_names:
        if name in TECH_TRIGGER_PLAYS:
            return TECH_TRIGGER_PLAYS[name]

    hook_angle = str(row.get("hook_angle") or "").lower()
    trigger_text = " ".join(str(item) for item in _safe_list(row.get("trigger_events"))).lower()
    title_text = str(row.get("title") or "").lower()
    persona = str(row.get("persona_type") or "").lower()

    if "hiring" in hook_angle or "[hiring]" in trigger_text or "posted job" in trigger_text:
        if persona in {"revops", "marketing_ops"}:
            return "new_marketing_ops_or_revops_leader"
        return "hiring_surge_in_demand_gen_or_abm"

    ai_text = " ".join(
        [
            hook_angle,
            trigger_text,
            str(row.get("brief_summary") or "").lower(),
            str(row.get("tech_context") or "").lower(),
            title_text,
        ]
    )
    if any(keyword in ai_text for keyword in (" ai ", "ai-", "agent", "prompt", "automation")):
        return "ai_forward_marketing_team"

    if persona in PERSONA_DEFAULT_PLAYS:
        return PERSONA_DEFAULT_PLAYS[persona]

    if row.get("cta_click_flag") or row.get("top_theme") or row.get("engagement_level") == "high":
        return "intent_surge_plus_engaged_content_path"

    return None


def _parse_trigger_events(row: dict[str, Any]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    pattern = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<text>.+)$")

    for raw in _safe_list(row.get("trigger_events")):
        text = str(raw or "").strip()
        if not text:
            continue
        match = pattern.match(text)
        tag = match.group("tag").upper() if match else "WEB"
        body = match.group("text") if match else text
        events.append(
            {
                "tag": tag,
                "text": _trim_text(body, max_len=180),
            }
        )

    company_snapshot = str(row.get("company_snapshot") or "").strip()
    if company_snapshot:
        first_sentence = re.split(r"(?<=[.!?])\s+", company_snapshot)[0]
        events.append({"tag": "SNAPSHOT", "text": _trim_text(first_sentence, max_len=180)})

    events.sort(key=lambda item: (TRIGGER_PRIORITY.get(item["tag"], 99), item["text"]))
    return _dedupe_preserve_order(events)


def _trigger_topic(tag: str) -> str | None:
    return {
        "HIRING": "team_capacity",
        "FUNDING": "growth_mandate",
        "PRODUCT": "product_launch",
        "EXEC": "leadership_change",
        "EVENT": "event_engagement",
        "PARTNER": "partner_motion",
    }.get(tag)


def _select_primary_asset(
    row: dict[str, Any],
    assets_by_id: dict[str, dict[str, Any]],
    assets_by_theme: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    top_asset_id = row.get("top_asset_id")
    if top_asset_id and top_asset_id in assets_by_id:
        return assets_by_id[top_asset_id]

    top_theme = row.get("top_theme")
    if top_theme:
        assets = assets_by_theme.get(str(top_theme), [])
        if assets:
            return assets[0]
    return None


def _build_topic_focus(
    row: dict[str, Any],
    play_id: str | None,
    primary_asset: dict[str, Any] | None,
    trigger_events: list[dict[str, str]],
) -> list[str]:
    topics: list[str] = []
    if row.get("top_theme"):
        topics.append(str(row["top_theme"]))

    topic_distribution = _safe_dict(row.get("topic_distribution_json"))
    for topic, _count in sorted(
        topic_distribution.items(),
        key=lambda item: (-float(item[1] or 0), str(item[0])),
    )[:2]:
        topics.append(str(topic))

    if primary_asset:
        for theme in _safe_list(primary_asset.get("themes")):
            topics.append(str(theme))

    if play_id and play_id in PLAY_LIBRARY:
        topics.extend(str(topic) for topic in PLAY_LIBRARY[play_id].get("topic_focus") or [])

    for event in trigger_events[:2]:
        topic = _trigger_topic(event["tag"])
        if topic:
            topics.append(topic)

    tech_names = [name.lower() for name in _extract_tech_names(row.get("apollo_raw"))]
    if any(name in COMPETITOR_TOOLS for name in tech_names):
        topics.append("competitive_context")
    if any(name in MAP_TOOLS for name in tech_names):
        topics.append("map_integration")

    return _dedupe_preserve_order([topic for topic in topics if topic])[:5]


def _build_why_now(
    row: dict[str, Any],
    primary_asset: dict[str, Any] | None,
    trigger_events: list[dict[str, str]],
) -> list[str]:
    reasons: list[str] = []
    theme_label = _theme_label(row.get("top_theme"))
    asset_title = str((primary_asset or {}).get("title") or "").strip()

    if row.get("cta_click_flag"):
        reasons.append(
            "Clicked through the Folloze experience, which is the clearest first-party sign they want a concrete next step."
        )
    elif row.get("multi_asset_same_theme_flag") and theme_label:
        reasons.append(
            f"Consumed multiple assets around {theme_label}, which suggests sustained interest in that workflow."
        )
    elif row.get("single_asset_deep_view_flag"):
        target = asset_title or (theme_label and f"{theme_label} content") or "one focused asset"
        reasons.append(f"Spent meaningful time with {target}, which is stronger than a light board skim.")
    elif row.get("repeat_visitor_flag"):
        days = row.get("days_visited") or 2
        reasons.append(f"Returned to the Folloze experience across {days} separate days, which suggests the topic is still live.")
    elif row.get("last_activity_at"):
        reasons.append("Recent Folloze engagement gives us a fresh first-party reason to follow up.")

    if asset_title and theme_label:
        reasons.append(f"Top content signal was {asset_title}, pointing to interest in {theme_label}.")
    elif theme_label:
        reasons.append(f"The strongest recent theme is {theme_label}.")

    if row.get("intent_strength") and float(row["intent_strength"] or 0) >= 4:
        reasons.append("More than one engagement clue is clustering at the account level, not just a single isolated touch.")

    for event in trigger_events[:2]:
        reasons.append(f"{_humanize_token(event['tag'])} signal: {event['text']}")

    return _dedupe_preserve_order(reasons)[:4]


def _build_why_them(row: dict[str, Any], play_id: str | None, topic_focus: list[str]) -> str:
    company = row.get("company_name") or row.get("domain") or "the account"
    title = row.get("title") or _humanize_token(row.get("persona_type")) or "the contact"
    hook_angle = str(row.get("hook_angle") or "").strip()
    theme_label = _theme_label(row.get("top_theme"))

    if hook_angle:
        return f"{title} at {company}. {hook_angle}"

    if theme_label:
        return f"{title} is a likely owner or strong influencer for {theme_label} work at {company}."

    if play_id == "new_marketing_ops_or_revops_leader":
        return f"{title} is close to the signal, workflow, and handoff problems Folloze is designed to tighten at {company}."
    if play_id == "new_abm_leader":
        return f"{title} likely owns or shapes how {company} scales buying-group engagement and account experiences."
    if play_id == "new_demand_gen_leader":
        return f"{title} is likely accountable for turning engagement into pipeline efficiently at {company}."

    if topic_focus:
        return f"{title} is a plausible owner or influencer for {', '.join(_humanize_token(topic) for topic in topic_focus[:2])} at {company}."

    return f"{title} is a plausible owner or influencer for the next-stage follow-up motion at {company}."


def _build_proof_points(
    row: dict[str, Any],
    play_id: str | None,
    primary_asset: dict[str, Any] | None,
    trigger_events: list[dict[str, str]],
    primitives: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []

    if play_id and play_id in PLAY_LIBRARY:
        for text in PLAY_LIBRARY[play_id].get("proof_points") or []:
            proofs.append(
                {
                    "text": text,
                    "source_type": "play_library",
                    "source_ref": play_id,
                }
            )

    if primary_asset:
        if primary_asset.get("followup_angle"):
            proofs.append(
                {
                    "text": _trim_text(primary_asset["followup_angle"], max_len=180),
                    "source_type": "asset_catalog",
                    "source_ref": primary_asset.get("asset_id"),
                }
            )
        elif primary_asset.get("summary"):
            proofs.append(
                {
                    "text": _trim_text(primary_asset["summary"], max_len=180),
                    "source_type": "asset_catalog",
                    "source_ref": primary_asset.get("asset_id"),
                }
            )

    tech_names = [name.lower() for name in _extract_tech_names(row.get("apollo_raw"))]
    competitor = next((name for name in tech_names if name in COMPETITOR_TOOLS), None)
    if competitor:
        proofs.append(
            {
                "text": f"Folloze can carry engagement beyond intent data and into a tailored journey that sales can act on, which matters when {competitor.title()} is already in the mix.",
                "source_type": "technographic",
                "source_ref": competitor,
            }
        )

    map_tool = next((name for name in tech_names if name in MAP_TOOLS), None)
    if map_tool:
        proofs.append(
            {
                "text": f"Fits alongside {map_tool.title()} rather than forcing a rip-and-replace motion.",
                "source_type": "technographic",
                "source_ref": map_tool,
            }
        )

    for primitive in primitives[:2]:
        proofs.append(
            {
                "text": _trim_text(primitive.get("text"), max_len=180),
                "source_type": "messaging_primitive",
                "source_ref": primitive.get("primitive_id"),
            }
        )

    for fact in facts[:1]:
        proofs.append(
            {
                "text": _trim_text(fact.get("fact_text"), max_len=180),
                "source_type": "fact_graph",
                "source_ref": fact.get("fact_graph_event_id"),
            }
        )

    for event in trigger_events[:1]:
        proofs.append(
            {
                "text": _trim_text(event["text"], max_len=180),
                "source_type": "research_trigger",
                "source_ref": event["tag"],
            }
        )

    return _dedupe_preserve_order([proof for proof in proofs if proof.get("text")])[:4]


def _build_content_recommendations(
    row: dict[str, Any],
    primary_asset: dict[str, Any] | None,
    assets_by_theme: dict[str, list[dict[str, Any]]],
    play_id: str | None,
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    top_theme = str(row.get("top_theme") or "")

    if primary_asset:
        recommendations.append(
            {
                "type": "asset",
                "asset_id": primary_asset.get("asset_id"),
                "title": primary_asset.get("title"),
                "summary": primary_asset.get("summary"),
                "destination_board": primary_asset.get("destination_board") or row.get("recommended_destination_board"),
                "reason": primary_asset.get("followup_angle") or "Closest content match to the signal state.",
            }
        )

    if top_theme:
        for candidate in assets_by_theme.get(top_theme, []):
            if primary_asset and candidate.get("asset_id") == primary_asset.get("asset_id"):
                continue
            recommendations.append(
                {
                    "type": "asset",
                    "asset_id": candidate.get("asset_id"),
                    "title": candidate.get("title"),
                    "summary": candidate.get("summary"),
                    "destination_board": candidate.get("destination_board") or row.get("recommended_destination_board"),
                    "reason": "Alternative content in the same theme cluster.",
                }
            )
            break

    destination_board = row.get("recommended_destination_board") or (primary_asset or {}).get("destination_board")
    if destination_board:
        recommendations.append(
            {
                "type": "board",
                "board_id": destination_board,
                "play_id": play_id,
                "reason": "Board destination suggested by current signal state.",
            }
        )

    return _dedupe_preserve_order(recommendations)[:3]


def _build_cta(row: dict[str, Any], play_id: str | None, topic_focus: list[str]) -> str:
    theme_label = _theme_label(row.get("top_theme")) or (
        _humanize_token(topic_focus[0]) if topic_focus else "the workflow"
    )
    angle = row.get("recommended_next_angle")
    cta_style = PLAY_LIBRARY.get(play_id or "", {}).get("cta_style")

    if angle == "cta_click_followup":
        return f"Worth sending the short {theme_label} board so they can react to a concrete next step asynchronously?"
    if angle == "multi_asset_theme_followup":
        return f"Open to a quick look at how teams use Folloze for {theme_label} once that topic starts getting repeat engagement?"
    if angle == "deep_asset_followup":
        return f"Useful if I send the example board behind the {theme_label} workflow rather than jumping straight to a meeting?"
    if angle == "repeat_visitor_followup":
        return f"Would it be useful to send the concise {theme_label} board and let them decide if a deeper conversation is worth it?"
    if angle == "board_recap_followup":
        return f"Okay to send a short {theme_label} recap board instead of a generic deck?"

    if cta_style == "competitive_first":
        return f"Worth showing how Folloze complements the current stack while giving the team a cleaner {theme_label} follow-up path?"
    if cta_style == "technical_first":
        return f"Useful if I send the technical board that shows the {theme_label} workflow end to end?"
    return f"Worth sharing the short {theme_label} board so they can react before committing to a call?"


def _build_forbidden_claims(row: dict[str, Any]) -> list[str]:
    tech_names = [name.lower() for name in _extract_tech_names(row.get("apollo_raw"))]
    claims = [
        "Do not claim they are actively evaluating vendors unless explicit evidence exists.",
        "Do not mention hidden tracking details, exact timestamps, or time-on-page numbers.",
        "Do not overstate one contact's engagement as buying-committee consensus.",
        "Do not present generic enrichment as first-party evidence.",
    ]
    if not any(name in COMPETITOR_TOOLS for name in tech_names):
        claims.append("Do not mention a competitor or stack claim that is not actually in evidence.")
    return _dedupe_preserve_order(claims)


def _select_tone_profile(row: dict[str, Any]) -> str:
    persona = str(row.get("persona_type") or "").lower()
    if persona == "gtm_engineer":
        base = "technical_peer"
    elif persona in {"revops", "marketing_ops"}:
        base = "operator"
    elif persona == "cmo":
        base = "executive"
    else:
        base = "consultative"

    if row.get("cta_click_flag") or row.get("engagement_level") == "high":
        return f"{base}_direct"
    return f"{base}_light"


def _evaluate_send_gate(
    row: dict[str, Any],
    *,
    play_id: str | None,
    primary_asset: dict[str, Any] | None,
    trigger_events: list[dict[str, str]],
    primitives: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    last_activity_at = row.get("last_activity_at")
    if isinstance(last_activity_at, datetime):
        fresh_first_party_signal = (now_utc - last_activity_at.astimezone(timezone.utc)).total_seconds() <= (
            FIRST_PARTY_FRESHNESS_DAYS * 86400
        )
    else:
        fresh_first_party_signal = False

    evidence_markers: list[str] = []

    if row.get("cta_click_flag") and fresh_first_party_signal:
        evidence_markers.append("cta_click")
    if row.get("multi_asset_same_theme_flag") and fresh_first_party_signal:
        evidence_markers.append("multi_asset_same_theme")
    elif row.get("single_asset_deep_view_flag") and fresh_first_party_signal:
        evidence_markers.append("single_asset_deep_view")
    elif row.get("repeat_visitor_flag") and fresh_first_party_signal:
        evidence_markers.append("repeat_visitor")
    elif row.get("engagement_level") in {"low", "medium", "high"} and fresh_first_party_signal:
        evidence_markers.append("recent_first_party_engagement")

    if primary_asset and fresh_first_party_signal:
        evidence_markers.append("resolved_asset")
    if trigger_events:
        evidence_markers.append("trigger_event")
    if primitives:
        evidence_markers.append("messaging_primitive")
    if facts:
        evidence_markers.append("fact_graph")
    if float(row.get("intent_strength") or 0) >= 4:
        evidence_markers.append("account_intent_cluster")

    evidence_markers = _dedupe_preserve_order(evidence_markers)

    angle_markers: list[str] = []
    if play_id:
        angle_markers.append(f"play:{play_id}")
    if row.get("recommended_next_angle"):
        angle_markers.append(f"angle:{row['recommended_next_angle']}")
    if row.get("top_theme"):
        angle_markers.append(f"theme:{row['top_theme']}")
    if str(row.get("hook_angle") or "").strip():
        angle_markers.append("hook_angle")

    evidence_points = len(evidence_markers)
    gate_reasons: list[str] = []

    if not fresh_first_party_signal:
        gate_reasons.append(
            f"No fresh first-party signal in the last {FIRST_PARTY_FRESHNESS_DAYS} days."
        )
    if evidence_points < 2:
        gate_reasons.append(f"Only {evidence_points} credible evidence point(s); need at least 2.")
    if not angle_markers:
        gate_reasons.append("No clear message angle selected from play, theme, or follow-up angle.")

    if gate_reasons:
        return {
            "status": "blocked",
            "evidence_points": evidence_points,
            "evidence_markers": evidence_markers,
            "angle_markers": angle_markers,
            "reasons": gate_reasons,
        }

    return {
        "status": "passed",
        "evidence_points": evidence_points,
        "evidence_markers": evidence_markers,
        "angle_markers": angle_markers,
        "reasons": [
            f"{evidence_points} credible evidence point(s) support follow-up.",
            f"Message angle available via {angle_markers[0]}.",
        ],
    }


def _brief_payload_for_compare(brief: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": brief.get("contact_id"),
        "account_id": brief.get("account_id"),
        "why_now": brief.get("why_now") or [],
        "why_them": brief.get("why_them") or "",
        "topic_focus": brief.get("topic_focus") or [],
        "proof_points_json": brief.get("proof_points_json") or [],
        "content_recommendation_json": brief.get("content_recommendation_json") or [],
        "asset_ids_used": brief.get("asset_ids_used") or [],
        "primitive_ids_used": brief.get("primitive_ids_used") or [],
        "fact_ids_used": brief.get("fact_ids_used") or [],
        "evidence_points": brief.get("evidence_points"),
        "gate_status": brief.get("gate_status") or "",
        "gate_reasons_json": brief.get("gate_reasons_json") or [],
        "cta": brief.get("cta") or "",
        "forbidden_claims_json": brief.get("forbidden_claims_json") or [],
        "tone_profile": brief.get("tone_profile") or "",
        "play_id": brief.get("play_id"),
    }


def _compute_brief_fingerprint(brief: dict[str, Any]) -> str:
    canonical = json.dumps(
        _brief_payload_for_compare(brief),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _brief_has_changed(latest: dict[str, Any] | None, new_brief: dict[str, Any]) -> bool:
    if not latest:
        return True
    latest_fingerprint = latest.get("brief_fingerprint") or _compute_brief_fingerprint(latest)
    new_fingerprint = new_brief.get("brief_fingerprint") or _compute_brief_fingerprint(new_brief)
    return latest_fingerprint != new_fingerprint


def _build_outbound_brief(
    row: dict[str, Any],
    *,
    assets_by_id: dict[str, dict[str, Any]],
    assets_by_theme: dict[str, list[dict[str, Any]]],
    primitives: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    play_id = _select_play_id(row)
    trigger_events = _parse_trigger_events(row)
    primary_asset = _select_primary_asset(row, assets_by_id, assets_by_theme)
    topic_focus = _build_topic_focus(row, play_id, primary_asset, trigger_events)
    why_now = _build_why_now(row, primary_asset, trigger_events)
    why_them = _build_why_them(row, play_id, topic_focus)
    proof_points = _build_proof_points(row, play_id, primary_asset, trigger_events, primitives, facts)
    content_recommendation = _build_content_recommendations(row, primary_asset, assets_by_theme, play_id)
    gate = _evaluate_send_gate(
        row,
        play_id=play_id,
        primary_asset=primary_asset,
        trigger_events=trigger_events,
        primitives=primitives,
        facts=facts,
        now_utc=now_utc,
    )

    asset_ids_used = []
    if primary_asset and primary_asset.get("asset_id"):
        asset_ids_used.append(str(primary_asset["asset_id"]))
    asset_ids_used.extend(
        str(item["asset_id"])
        for item in content_recommendation
        if item.get("type") == "asset" and item.get("asset_id")
    )

    brief = {
        "account_id": row.get("account_id"),
        "contact_id": row["contact_id"],
        "source_state_updated_at": row.get("signal_state_updated_at"),
        "why_now": why_now,
        "why_them": why_them,
        "topic_focus": topic_focus,
        "proof_points_json": proof_points,
        "content_recommendation_json": content_recommendation,
        "asset_ids_used": _dedupe_preserve_order(asset_ids_used),
        "primitive_ids_used": _dedupe_preserve_order(
            [str(item["primitive_id"]) for item in primitives[:2] if item.get("primitive_id")]
        ),
        "fact_ids_used": _dedupe_preserve_order(
            [str(item["fact_graph_event_id"]) for item in facts[:1] if item.get("fact_graph_event_id")]
        ),
        "evidence_points": gate["evidence_points"],
        "gate_status": gate["status"],
        "gate_reasons_json": gate["reasons"],
        "cta": _build_cta(row, play_id, topic_focus),
        "forbidden_claims_json": _build_forbidden_claims(row),
        "tone_profile": _select_tone_profile(row),
        "play_id": play_id,
    }
    brief["brief_fingerprint"] = _compute_brief_fingerprint(brief)
    return brief


def _fetch_target_rows(conn, *, limit: int, contact_id_filter: str | None) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT
                css.contact_id::text,
                css.account_id::text,
                c.first_name,
                c.last_name,
                c.title,
                c.persona_type,
                c.email,
                a.company_name,
                a.domain,
                a.segment,
                a.industry,
                a.employee_count,
                a.apollo_raw,
                css.last_activity_at,
                css.engagement_level,
                css.top_theme,
                css.top_asset_id,
                css.top_asset_summary,
                css.repeat_visitor_flag,
                css.cta_click_flag,
                css.single_asset_deep_view_flag,
                css.multi_asset_same_theme_flag,
                css.days_visited,
                css.number_of_items_viewed,
                css.total_view_time_seconds,
                css.signal_confidence,
                css.recommended_next_angle,
                css.recommended_destination_board,
                css.recommended_play_id,
                css.state_payload_json,
                css.updated_at AS signal_state_updated_at,
                ass.intent_strength,
                ass.topic_distribution_json,
                ass.recommended_play_id AS account_recommended_play_id,
                cr.hook_angle,
                cr.company_snapshot,
                cr.people_intel,
                cr.trigger_events,
                cr.tech_context,
                cr.competitive_notes,
                cr.brief_summary
            FROM contact_signal_state css
            JOIN contacts c
              ON c.contact_id = css.contact_id
            LEFT JOIN accounts a
              ON a.account_id = css.account_id
            LEFT JOIN account_signal_state ass
              ON ass.account_id = css.account_id
            LEFT JOIN contact_research cr
              ON cr.contact_id = css.contact_id
            WHERE css.engagement_level IS NOT NULL
              AND (%s IS NULL OR css.contact_id = %s::uuid)
            ORDER BY css.updated_at DESC
            LIMIT %s
            """,
            (contact_id_filter, contact_id_filter, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def _fetch_assets(conn) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT
                asset_id,
                board_id,
                title,
                summary,
                themes,
                followup_angle,
                destination_board,
                status
            FROM asset_catalog
            WHERE COALESCE(status, 'active') <> 'archived'
            ORDER BY title ASC NULLS LAST, asset_id ASC
            """
        )
        rows = [dict(row) for row in cur.fetchall()]

    assets_by_id = {row["asset_id"]: row for row in rows}
    assets_by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for theme in _safe_list(row.get("themes")):
            if theme:
                assets_by_theme[str(theme)].append(row)
    return assets_by_id, dict(assets_by_theme)


def _fetch_primitives(conn, *, contact_ids: list[str], account_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not contact_ids and not account_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT
                primitive_id::text,
                contact_id::text,
                account_id::text,
                primitive_type,
                text,
                confidence,
                expires_at
            FROM messaging_primitives
            WHERE (
                contact_id = ANY(%s::uuid[])
                OR account_id = ANY(%s::uuid[])
            )
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY confidence DESC NULLS LAST, created_at DESC
            """,
            (contact_ids or [], account_ids or []),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cur.fetchall():
            item = dict(row)
            if item.get("contact_id"):
                grouped[f"contact:{item['contact_id']}"].append(item)
            if item.get("account_id"):
                grouped[f"account:{item['account_id']}"].append(item)
        return grouped


def _fetch_facts(conn, *, contact_ids: list[str], account_ids: list[str], max_age_days: int = 180) -> dict[str, list[dict[str, Any]]]:
    if not contact_ids and not account_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT
                fact_graph_event_id::text,
                entity_type,
                entity_id,
                fact_type,
                fact_text,
                source_url,
                source_name,
                observed_at,
                confidence,
                is_foundational_firmographic
            FROM fact_graph_events
            WHERE (
                (entity_type = 'contact' AND entity_id = ANY(%s))
                OR (entity_type = 'account' AND entity_id = ANY(%s))
            )
              AND (
                is_foundational_firmographic = TRUE
                OR observed_at IS NULL
                OR observed_at >= NOW() - (%s || ' days')::interval
              )
              AND (expiry_at IS NULL OR expiry_at > NOW())
            ORDER BY observed_at DESC NULLS LAST, created_at DESC
            """,
            (contact_ids or [], account_ids or [], max_age_days),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cur.fetchall():
            item = dict(row)
            grouped[f"{item['entity_type']}:{item['entity_id']}"].append(item)
        return grouped


def _fetch_latest_briefs(conn, *, contact_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not contact_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (contact_id)
                brief_id::text,
                contact_id::text,
                account_id::text,
                source_state_updated_at,
                why_now,
                why_them,
                topic_focus,
                proof_points_json,
                content_recommendation_json,
                asset_ids_used,
                primitive_ids_used,
                fact_ids_used,
                evidence_points,
                gate_status,
                gate_reasons_json,
                cta,
                forbidden_claims_json,
                tone_profile,
                play_id,
                brief_fingerprint,
                created_at
            FROM outbound_briefs
            WHERE contact_id = ANY(%s::uuid[])
            ORDER BY contact_id, created_at DESC
            """,
            (contact_ids,),
        )
        return {row["contact_id"]: dict(row) for row in cur.fetchall()}


def _insert_brief(conn, brief: dict[str, Any]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbound_briefs (
                account_id,
                contact_id,
                source_state_updated_at,
                brief_fingerprint,
                why_now,
                why_them,
                topic_focus,
                proof_points_json,
                content_recommendation_json,
                asset_ids_used,
                primitive_ids_used,
                fact_ids_used,
                evidence_points,
                gate_status,
                gate_reasons_json,
                cta,
                forbidden_claims_json,
                tone_profile,
                play_id
            )
            VALUES (
                %(account_id)s::uuid,
                %(contact_id)s::uuid,
                %(source_state_updated_at)s,
                %(brief_fingerprint)s,
                %(why_now)s::jsonb,
                %(why_them)s,
                %(topic_focus)s::jsonb,
                %(proof_points_json)s::jsonb,
                %(content_recommendation_json)s::jsonb,
                %(asset_ids_used)s::text[],
                %(primitive_ids_used)s::text[],
                %(fact_ids_used)s::text[],
                %(evidence_points)s,
                %(gate_status)s,
                %(gate_reasons_json)s::jsonb,
                %(cta)s,
                %(forbidden_claims_json)s::jsonb,
                %(tone_profile)s,
                %(play_id)s
            )
            ON CONFLICT DO NOTHING
            """,
            {
                **brief,
                "why_now": json.dumps(brief["why_now"]),
                "topic_focus": json.dumps(brief["topic_focus"]),
                "proof_points_json": json.dumps(brief["proof_points_json"]),
                "content_recommendation_json": json.dumps(brief["content_recommendation_json"]),
                "asset_ids_used": brief["asset_ids_used"],
                "primitive_ids_used": brief["primitive_ids_used"],
                "fact_ids_used": brief["fact_ids_used"],
                "gate_reasons_json": json.dumps(brief["gate_reasons_json"]),
                "forbidden_claims_json": json.dumps(brief["forbidden_claims_json"]),
            },
        )
        return cur.rowcount == 1


def run(*, dry_run: bool = False, limit: int = 100, contact_id_filter: str | None = None) -> None:
    logger = configure_logging("build_outbound_briefs")

    with db_conn() as conn:
        if not try_advisory_xact_lock(conn, BUILD_OUTBOUND_BRIEFS_LOCK):
            logger.info("Skipping run because another build_outbound_briefs transaction already holds the advisory lock.")
            return

        now_utc = datetime.now(timezone.utc)
        rows = _fetch_target_rows(conn, limit=limit, contact_id_filter=contact_id_filter)
        contact_ids = [row["contact_id"] for row in rows]
        account_ids = [row["account_id"] for row in rows if row.get("account_id")]
        assets_by_id, assets_by_theme = _fetch_assets(conn)
        primitives_by_entity = _fetch_primitives(conn, contact_ids=contact_ids, account_ids=account_ids)
        facts_by_entity = _fetch_facts(conn, contact_ids=contact_ids, account_ids=account_ids)
        latest_briefs = _fetch_latest_briefs(conn, contact_ids=contact_ids)

        inserted = 0
        unchanged = 0
        duplicate_skipped = 0

        for row in rows:
            entity_primitives = [
                *primitives_by_entity.get(f"contact:{row['contact_id']}", []),
                *primitives_by_entity.get(f"account:{row.get('account_id')}", []),
            ]
            entity_facts = [
                *facts_by_entity.get(f"contact:{row['contact_id']}", []),
                *facts_by_entity.get(f"account:{row.get('account_id')}", []),
            ]
            brief = _build_outbound_brief(
                row,
                assets_by_id=assets_by_id,
                assets_by_theme=assets_by_theme,
                primitives=entity_primitives,
                facts=entity_facts,
                now_utc=now_utc,
            )

            if not _brief_has_changed(latest_briefs.get(row["contact_id"]), brief):
                unchanged += 1
                continue

            if dry_run:
                logger.info(
                    "contact=%s play=%s gate=%s evidence=%s theme=%s why_now=%s cta=%s",
                    row.get("email") or row["contact_id"],
                    brief.get("play_id"),
                    brief.get("gate_status"),
                    brief.get("evidence_points"),
                    row.get("top_theme"),
                    " | ".join(brief["why_now"]),
                    brief["cta"],
                )
            else:
                if _insert_brief(conn, brief):
                    inserted += 1
                    latest_briefs[row["contact_id"]] = brief
                else:
                    duplicate_skipped += 1
                    unchanged += 1
                    continue
            if dry_run:
                inserted += 1

    logger.info(
        "Done. processed=%s inserted=%s unchanged=%s duplicate_skipped=%s dry_run=%s",
        len(rows),
        inserted,
        unchanged,
        duplicate_skipped,
        dry_run,
    )
