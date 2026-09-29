"""
Tests for suppression logic — the gate preventing emails to customers and
suppressed contacts.

Tests cover the pure-Python parts of the suppression chain:
  - Domain extraction in crm_client.py
  - Score/segment logic that determines eligibility
  - objection_model and contact_scorer (no DB required)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from scoring.account_scorer import (
    compute_fit_score,
    compute_timing_score,
    compute_priority_score,
    assign_segment,
    _recency_boost,
)
from scoring.contact_scorer import infer_trajectory
from personalization.objection_model import predict_objection
from connectors.crm_client import _clean_domain, _escape_sf_value


# ── _clean_domain (CRM suppression extraction) ───────────────────────────────

def test_clean_domain_strips_https():
    assert _clean_domain("https://acme.com") == "acme.com"


def test_clean_domain_strips_http():
    assert _clean_domain("http://acme.com") == "acme.com"


def test_clean_domain_strips_www():
    assert _clean_domain("https://www.acme.com") == "acme.com"


def test_clean_domain_strips_path():
    assert _clean_domain("https://acme.com/products/thing") == "acme.com"


def test_clean_domain_strips_trailing_slash():
    assert _clean_domain("https://acme.com/") == "acme.com"


def test_clean_domain_bare():
    assert _clean_domain("acme.com") == "acme.com"


def test_clean_domain_empty():
    assert _clean_domain("") == ""
    assert _clean_domain(None) == ""


def test_escape_sf_value_quotes():
    assert _escape_sf_value("O'Brien") == "O\\'Brien"


def test_escape_sf_value_backslash():
    assert _escape_sf_value(r"foo\bar") == r"foo\\bar"


# ── assign_segment ────────────────────────────────────────────────────────────

def test_segment_ent():
    assert assign_segment(1500) == "ENT"
    assert assign_segment(1001) == "ENT"


def test_segment_mm():
    assert assign_segment(500) == "MM"
    assert assign_segment(100) == "MM"
    assert assign_segment(1000) == "MM"


def test_segment_smb():
    assert assign_segment(50) == "SMB"
    assert assign_segment(99) == "SMB"


def test_segment_none_defaults_mm():
    assert assign_segment(None) == "MM"


# ── compute_fit_score ─────────────────────────────────────────────────────────

def test_fit_score_full_icp():
    account = {
        "industry": "marketing technology",
        "employee_count": 500,
        "tech_stack": [{"name": "Marketo"}, {"name": "6sense"}],
        "keywords": [],
    }
    score = compute_fit_score(account)
    assert score >= 9.0  # industry(3) + headcount(3) + tech(4)


def test_fit_score_no_match():
    account = {
        "industry": "agriculture",
        "employee_count": 10,
        "tech_stack": [],
        "keywords": [],
    }
    score = compute_fit_score(account)
    assert score == 0.0


def test_fit_score_capped_at_ten():
    account = {
        "industry": "saas",
        "employee_count": 500,
        "tech_stack": [{"name": "Marketo"}, {"name": "6sense"}, {"name": "Demandbase"}, {"name": "HubSpot"}],
        "keywords": [],
    }
    score = compute_fit_score(account)
    assert score <= 10.0


# ── compute_timing_score ──────────────────────────────────────────────────────

def test_timing_score_freeze_penalty():
    account = {
        "keywords": ["layoffs", "restructuring"],
        "short_description": "",
        "latest_funding_round_date": "",
        "latest_funding_round_type": "",
    }
    score = compute_timing_score(account)
    assert score == 0.0  # negative capped at 0


def test_timing_score_hiring_signal():
    account = {
        "keywords": ["demand generation", "ABM"],
        "short_description": "",
        "latest_funding_round_date": "",
        "latest_funding_round_type": "",
    }
    score = compute_timing_score(account)
    assert score >= 3.0


# ── _recency_boost ────────────────────────────────────────────────────────────

def test_recency_boost_none():
    assert _recency_boost(None) == 0.0


def test_recency_boost_very_old():
    assert _recency_boost("2020-01-01T00:00:00+00:00") == 0.0


def test_recency_boost_recent():
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert _recency_boost(recent) == 2.0


def test_recency_boost_within_30_days():
    from datetime import datetime, timezone, timedelta
    mid = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    assert _recency_boost(mid) == 1.0


# ── infer_trajectory ──────────────────────────────────────────────────────────

def test_trajectory_newly_hired():
    from datetime import datetime, timezone, timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m")
    contact = {
        "apollo_raw": {
            "employment_history": [{"start_date": start, "current": True}]
        }
    }
    assert infer_trajectory(contact) == "newly_hired"


def test_trajectory_established():
    from datetime import datetime, timezone, timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=900)).strftime("%Y-%m")
    contact = {
        "apollo_raw": {
            "employment_history": [{"start_date": start, "current": True}]
        }
    }
    assert infer_trajectory(contact) == "established"


def test_trajectory_unknown_no_data():
    assert infer_trajectory({}) == "unknown"
    assert infer_trajectory({"apollo_raw": {}}) == "unknown"


# ── predict_objection ─────────────────────────────────────────────────────────

def test_objection_budget_smb():
    account = {"segment": "SMB", "signals": {}, "apollo_raw": {}}
    contact = {"title": "Marketing Manager", "seniority": "manager"}
    assert predict_objection(account, contact) == "budget"


def test_objection_wrong_persona():
    account = {"segment": "MM", "signals": {}, "apollo_raw": {}}
    contact = {"title": "Marketing Coordinator", "seniority": "entry"}
    assert predict_objection(account, contact) == "wrong_persona"


def test_objection_none_ent_with_signals():
    account = {
        "segment": "ENT",
        "signals": {"hiring": True},
        "apollo_raw": {"latest_funding_round_date": "2025-10-01"},
    }
    contact = {"title": "VP Revenue Marketing", "seniority": "vp"}
    result = predict_objection(account, contact)
    assert result in ("none", "priorities")  # ENT with signals shouldn't be budget/timing/wrong_persona
