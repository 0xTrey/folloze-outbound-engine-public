import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.build_outbound_briefs import (  # noqa: E402
    _brief_has_changed,
    _build_outbound_brief,
    _select_play_id,
)


def _asset_maps():
    assets = {
        "2211729": {
            "asset_id": "2211729",
            "title": "Build a Persona-Based Nurture Campaign - Campaign Agent",
            "summary": "Example nurture asset showing tailored messaging across multiple touches.",
            "themes": ["nurture_orchestration"],
            "followup_angle": "Use this when the buyer is thinking about multi-touch nurture rather than one-off campaigns.",
            "destination_board": "238181",
        },
        "2211726": {
            "asset_id": "2211726",
            "title": "Build a 1:Many Account Campaign - Campaign Agent",
            "summary": "Video demo showing how Campaign Agent builds a 1:many account campaign.",
            "themes": ["campaign_automation"],
            "followup_angle": "Use this when the buyer wants to scale personalization across target accounts.",
            "destination_board": "238181",
        },
    }
    return assets, {
        "nurture_orchestration": [assets["2211729"]],
        "campaign_automation": [assets["2211726"]],
    }


def test_build_outbound_brief_uses_signal_state_asset_and_research():
    assets_by_id, assets_by_theme = _asset_maps()
    now_utc = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)
    row = {
        "contact_id": "contact-1",
        "account_id": "account-1",
        "first_name": "Pat",
        "last_name": "Example",
        "title": "GTM Engineer",
        "persona_type": "gtm_engineer",
        "email": "gopal@example.com",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "segment": "GTME",
        "apollo_raw": {
            "current_technologies": [
                {"name": "6sense"},
                {"name": "Marketo"},
            ]
        },
        "engagement_level": "high",
        "top_theme": "nurture_orchestration",
        "top_asset_id": "2211729",
        "top_asset_summary": "Example nurture asset showing tailored messaging across multiple touches.",
        "repeat_visitor_flag": False,
        "cta_click_flag": True,
        "single_asset_deep_view_flag": False,
        "multi_asset_same_theme_flag": False,
        "days_visited": 2,
        "number_of_items_viewed": 3,
        "total_view_time_seconds": 240,
        "signal_confidence": 0.8,
        "recommended_next_angle": "cta_click_followup",
        "recommended_destination_board": "238181",
        "recommended_play_id": "intent_surge_plus_engaged_content_path",
        "state_payload_json": {"evidence_points": 3},
        "signal_state_updated_at": now_utc - timedelta(hours=1),
        "last_activity_at": now_utc - timedelta(hours=2),
        "intent_strength": 5.0,
        "topic_distribution_json": {"nurture_orchestration": 2},
        "account_recommended_play_id": None,
        "hook_angle": "Hiring into the exact pain Folloze solves - execution capacity gap.",
        "company_snapshot": "ExampleCo is scaling outbound execution and tightening the GTM handoff.",
        "people_intel": "",
        "trigger_events": [
            "[HIRING] Posted lifecycle marketing and demand generation roles in the last 30 days.",
            "[PRODUCT] Rolled out a new outbound workflow focused on multi-touch execution.",
        ],
        "tech_context": "",
        "competitive_notes": "",
        "brief_summary": "",
    }

    brief = _build_outbound_brief(
        row,
        assets_by_id=assets_by_id,
        assets_by_theme=assets_by_theme,
        primitives=[],
        facts=[],
        now_utc=now_utc,
    )

    assert brief["play_id"] == "intent_surge_plus_engaged_content_path"
    assert any("Clicked through the Folloze experience" in reason for reason in brief["why_now"])
    assert any("HIRING signal" in reason for reason in brief["why_now"])
    assert "nurture_orchestration" in brief["topic_focus"]
    assert any(item.get("asset_id") == "2211729" for item in brief["content_recommendation_json"])
    assert any(point.get("source_type") == "asset_catalog" for point in brief["proof_points_json"])
    assert any("hidden tracking details" in claim.lower() for claim in brief["forbidden_claims_json"])
    assert brief["tone_profile"] == "technical_peer_direct"
    assert brief["gate_status"] == "passed"
    assert brief["evidence_points"] >= 2
    assert "2211729" in brief["asset_ids_used"]


def test_build_outbound_brief_blocks_when_evidence_and_angle_are_too_thin():
    assets_by_id, assets_by_theme = _asset_maps()
    row = {
        "contact_id": "contact-2",
        "account_id": "account-2",
        "first_name": "Jordan",
        "last_name": "Example",
        "title": "Marketing Manager",
        "persona_type": "",
        "email": "jordan@example.com",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "segment": "MM",
        "apollo_raw": {},
        "engagement_level": None,
        "top_theme": None,
        "top_asset_id": None,
        "top_asset_summary": None,
        "repeat_visitor_flag": False,
        "cta_click_flag": False,
        "single_asset_deep_view_flag": False,
        "multi_asset_same_theme_flag": False,
        "days_visited": None,
        "number_of_items_viewed": None,
        "total_view_time_seconds": None,
        "signal_confidence": 0.1,
        "recommended_next_angle": None,
        "recommended_destination_board": None,
        "recommended_play_id": None,
        "state_payload_json": {"evidence_points": 0},
        "signal_state_updated_at": None,
        "intent_strength": 0.0,
        "topic_distribution_json": {},
        "account_recommended_play_id": None,
        "hook_angle": "",
        "company_snapshot": "",
        "people_intel": "",
        "trigger_events": [],
        "tech_context": "",
        "competitive_notes": "",
        "brief_summary": "",
        "last_activity_at": None,
    }

    brief = _build_outbound_brief(
        row,
        assets_by_id=assets_by_id,
        assets_by_theme=assets_by_theme,
        primitives=[],
        facts=[],
    )

    assert brief["gate_status"] == "blocked"
    assert brief["evidence_points"] == 0
    assert any("need at least 2" in reason for reason in brief["gate_reasons_json"])
    assert any("No clear message angle" in reason for reason in brief["gate_reasons_json"])


def test_select_play_id_falls_back_to_tech_trigger_before_persona_default():
    row = {
        "recommended_play_id": None,
        "account_recommended_play_id": None,
        "apollo_raw": {"current_technologies": [{"name": "PathFactory"}]},
        "hook_angle": "",
        "trigger_events": [],
        "title": "Director of Demand Generation",
        "persona_type": "revmktg_vp",
        "brief_summary": "",
        "tech_context": "",
        "cta_click_flag": False,
        "top_theme": None,
        "engagement_level": "medium",
    }

    assert _select_play_id(row) == "pathfactory_present"


def test_brief_has_changed_returns_false_when_payload_matches_latest():
    latest = {
        "contact_id": "contact-1",
        "account_id": "account-1",
        "source_state_updated_at": "2026-04-10T12:00:00+00:00",
        "why_now": ["Recent Folloze engagement gives us a fresh first-party reason to follow up."],
        "why_them": "Director of Demand Generation is likely accountable for turning engagement into pipeline efficiently at ExampleCo.",
        "topic_focus": ["pipeline_efficiency"],
        "proof_points_json": [{"text": "Example proof", "source_type": "play_library", "source_ref": "new_demand_gen_leader"}],
        "content_recommendation_json": [{"type": "board", "board_id": "238181", "reason": "Board destination suggested by current signal state."}],
        "cta": "Worth sharing the short Pipeline Efficiency board so they can react before committing to a call?",
        "forbidden_claims_json": ["Do not claim they are actively evaluating vendors unless explicit evidence exists."],
        "tone_profile": "consultative_light",
        "play_id": "new_demand_gen_leader",
    }

    new_brief = {
        **latest,
        "brief_id": "ignored",
        "source_state_updated_at": "2026-04-11T12:00:00+00:00",
    }

    assert _brief_has_changed(latest, new_brief) is False


def test_build_outbound_brief_blocks_stale_first_party_signal():
    assets_by_id, assets_by_theme = _asset_maps()
    now_utc = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)
    row = {
        "contact_id": "contact-3",
        "account_id": "account-3",
        "first_name": "Taylor",
        "last_name": "Example",
        "title": "VP Demand Gen",
        "persona_type": "revmktg_vp",
        "email": "taylor@example.com",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "segment": "MM",
        "apollo_raw": {},
        "engagement_level": "high",
        "top_theme": "campaign_automation",
        "top_asset_id": "2211726",
        "top_asset_summary": "Video demo showing how Campaign Agent builds a 1:many account campaign.",
        "repeat_visitor_flag": False,
        "cta_click_flag": True,
        "single_asset_deep_view_flag": False,
        "multi_asset_same_theme_flag": False,
        "days_visited": 2,
        "number_of_items_viewed": 2,
        "total_view_time_seconds": 120,
        "signal_confidence": 0.8,
        "recommended_next_angle": "cta_click_followup",
        "recommended_destination_board": "238181",
        "recommended_play_id": "intent_surge_plus_engaged_content_path",
        "state_payload_json": {"evidence_points": 3},
        "signal_state_updated_at": now_utc - timedelta(hours=1),
        "last_activity_at": now_utc - timedelta(days=16),
        "intent_strength": 5.0,
        "topic_distribution_json": {"campaign_automation": 2},
        "account_recommended_play_id": None,
        "hook_angle": "",
        "company_snapshot": "",
        "people_intel": "",
        "trigger_events": [],
        "tech_context": "",
        "competitive_notes": "",
        "brief_summary": "",
    }

    brief = _build_outbound_brief(
        row,
        assets_by_id=assets_by_id,
        assets_by_theme=assets_by_theme,
        primitives=[],
        facts=[],
        now_utc=now_utc,
    )

    assert brief["gate_status"] == "blocked"
    assert any("No fresh first-party signal" in reason for reason in brief["gate_reasons_json"])
