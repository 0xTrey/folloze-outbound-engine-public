import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.derive_contact_signal_state import _derive_contact_state  # noqa: E402


def test_derive_contact_state_uses_snapshot_repeat_visitor_and_cta():
    contact = {
        "contact_id": "contact-1",
        "account_id": "account-1",
        "email": "person@example.com",
        "title": "VP Demand Gen",
        "company_name": "ExampleCo",
    }
    events = [
        {
            "event_type": "engagement_snapshot",
            "event_timestamp_utc": datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            "content_id": None,
            "board_id": "238181",
            "campaign_id": "GTME_VIBE_MKT",
            "duration_seconds": 629,
            "payload_json": {
                "board_id": "238181",
                "number_of_days_visited": 3,
                "number_of_items_viewed": 4,
                "number_of_cta_clicks": 5,
                "total_view_time": 629,
                "most_viewed_item": "Build a Persona-Based Campaign — Campaign Agent",
                "cta_clicker": True,
            },
        }
    ]

    asset_map = {
        "2211725": {
            "asset_id": "2211725",
            "board_id": "238181",
            "title": "Build a Persona-Based Campaign — Campaign Agent",
            "title_aliases": [],
            "summary": "Example campaign asset showing tailored messaging by audience.",
            "themes": ["persona_personalization"],
            "destination_board": "238181",
        }
    }

    state = _derive_contact_state(contact, events, asset_map)

    assert state["cta_click_flag"] is True
    assert state["repeat_visitor_flag"] is True
    assert state["engagement_level"] == "high"
    assert state["recommended_next_angle"] == "cta_click_followup"
    assert state["recommended_play_id"] == "intent_surge_plus_engaged_content_path"
    assert state["top_asset_id"] == "2211725"
    assert state["top_theme"] == "persona_personalization"
    assert state["top_asset_summary"] == "Example campaign asset showing tailored messaging by audience."


def test_derive_contact_state_single_asset_deep_view():
    contact = {
        "contact_id": "contact-2",
        "account_id": "account-2",
        "email": "person2@example.com",
        "title": "GTM Engineer",
        "company_name": "ExampleCo",
    }
    events = [
        {
            "event_type": "item_view",
            "event_timestamp_utc": datetime(2026, 3, 23, 9, 21, tzinfo=timezone.utc),
            "content_id": "2211726",
            "board_id": "238181",
            "campaign_id": "GTME_VIBE_MKT",
            "duration_seconds": 72,
            "payload_json": {},
        }
    ]
    asset_map = {
        "2211726": {
            "asset_id": "2211726",
            "title": "Build a 1:Many Account Campaign — Campaign Agent",
            "summary": "Video on running a 1:many account campaign with AI assistance.",
            "themes": ["campaign_automation"],
            "destination_board": "238181",
        }
    }

    state = _derive_contact_state(contact, events, asset_map)

    assert state["single_asset_deep_view_flag"] is True
    assert state["engagement_level"] == "medium"
    assert state["top_asset_id"] == "2211726"
    assert state["top_theme"] == "campaign_automation"
    assert state["recommended_next_angle"] == "deep_asset_followup"


def test_derive_contact_state_multi_asset_same_theme():
    contact = {
        "contact_id": "contact-3",
        "account_id": "account-3",
        "email": "person3@example.com",
        "title": "ABM Lead",
        "company_name": "ExampleCo",
    }
    events = [
        {
            "event_type": "item_view",
            "event_timestamp_utc": datetime(2026, 3, 23, 9, 21, tzinfo=timezone.utc),
            "content_id": "asset-1",
            "board_id": "238181",
            "campaign_id": "GTME_VIBE_MKT",
            "duration_seconds": 40,
            "payload_json": {},
        },
        {
            "event_type": "item_view",
            "event_timestamp_utc": datetime(2026, 3, 23, 9, 24, tzinfo=timezone.utc),
            "content_id": "asset-2",
            "board_id": "238181",
            "campaign_id": "GTME_VIBE_MKT",
            "duration_seconds": 55,
            "payload_json": {},
        },
    ]
    asset_map = {
        "asset-1": {
            "asset_id": "asset-1",
            "title": "Asset 1",
            "summary": "Summary 1",
            "themes": ["abm_personalization"],
            "destination_board": "238181",
        },
        "asset-2": {
            "asset_id": "asset-2",
            "title": "Asset 2",
            "summary": "Summary 2",
            "themes": ["abm_personalization"],
            "destination_board": "238181",
        },
    }

    state = _derive_contact_state(contact, events, asset_map)

    assert state["multi_asset_same_theme_flag"] is True
    assert state["engagement_level"] == "high"
    assert state["top_theme"] == "abm_personalization"
    assert state["recommended_next_angle"] == "multi_asset_theme_followup"
