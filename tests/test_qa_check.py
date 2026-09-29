"""
Tests for personalization/generator.py::qa_check()

This is the primary gate preventing malformed copy from reaching Smartlead.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from personalization.generator import qa_check


def _valid_payload():
    return {
        "subject":     "Quick question on your ABM motion",
        "opener":      "Saw that Acme is running intent campaigns with Demandbase.",
        "why_now":     "With the recent Series B, scaling ABM to cover more accounts is the obvious next move.",
        "proof_point": "Approved customer evidence supports this operating model.",
        "cta":         "Worth a 20-min look?",
        "t2_body":     "Should I send the closest approved example, or close this out?",
    }


def _tokens():
    return {"first_name": "Alex", "company": "Acme"}


# ── Required fields ──────────────────────────────────────────────────────────

def test_passes_valid_payload():
    passed, reason = qa_check(_valid_payload(), _tokens())
    assert passed, reason


def test_fails_empty_subject():
    p = _valid_payload()
    p["subject"] = ""
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "subject" in reason


def test_fails_empty_opener():
    p = _valid_payload()
    p["opener"] = "   "
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "opener" in reason


def test_fails_missing_required_field():
    p = _valid_payload()
    del p["cta"]
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "cta" in reason


# ── Character limits ─────────────────────────────────────────────────────────

def test_fails_subject_over_limit():
    p = _valid_payload()
    p["subject"] = "x" * 61
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "subject" in reason and "60" in reason


def test_passes_subject_at_limit():
    p = _valid_payload()
    p["subject"] = "x" * 60
    passed, reason = qa_check(p, _tokens())
    assert passed, reason


def test_fails_opener_over_limit():
    p = _valid_payload()
    p["opener"] = "x" * 201
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "opener" in reason


def test_fails_t2_body_over_limit():
    p = _valid_payload()
    p["t2_body"] = "x" * 201
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "t2_body" in reason


# ── Placeholder detection ─────────────────────────────────────────────────────

def test_fails_bracket_placeholder():
    p = _valid_payload()
    p["opener"] = "Hi [first_name], I noticed your company is hiring."
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "placeholder" in reason


def test_fails_curly_placeholder():
    p = _valid_payload()
    p["why_now"] = "Given {company}'s recent funding, now is the time."
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "placeholder" in reason


# ── Flattery detection ────────────────────────────────────────────────────────

def test_fails_generic_flattery():
    p = _valid_payload()
    p["opener"] = "Huge fan of what you're building at Acme."
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "compliment" in reason


def test_fails_congrats_opener():
    p = _valid_payload()
    p["opener"] = "Congrats on the Series B — impressive milestone."
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "compliment" in reason


# ── Optional fields (case study anchors) ─────────────────────────────────────

def test_passes_empty_case_study_anchor():
    p = _valid_payload()
    p["t1_case_study_anchor"] = ""
    passed, reason = qa_check(p, _tokens())
    assert passed, reason


def test_fails_case_study_anchor_over_limit():
    p = _valid_payload()
    p["t1_case_study_anchor"] = "x" * 81
    passed, reason = qa_check(p, _tokens())
    assert not passed
    assert "t1_case_study_anchor" in reason


# ── GTME 3-touch: t3_body required ───────────────────────────────────────────

def test_gtme_fails_without_t3_body():
    p = _valid_payload()
    # t3_body missing
    passed, reason = qa_check(p, _tokens(), sequence_family="GTME_VIBE_MKT")
    assert not passed
    assert "t3_body" in reason


def test_gtme_fails_empty_t3_body():
    p = _valid_payload()
    p["t3_body"] = ""
    passed, reason = qa_check(p, _tokens(), sequence_family="GTME_VIBE_MKT")
    assert not passed
    assert "t3_body" in reason


def test_gtme_passes_with_valid_t3_body():
    p = _valid_payload()
    p["t3_body"] = "Last note — happy to send over a 5-min Loom showing how this works."
    passed, reason = qa_check(p, _tokens(), sequence_family="GTME_VIBE_MKT")
    assert passed, reason


def test_gtme_fails_t3_body_over_limit():
    p = _valid_payload()
    p["t3_body"] = "x" * 151
    passed, reason = qa_check(p, _tokens(), sequence_family="GTME_VIBE_MKT")
    assert not passed
    assert "t3_body" in reason


def test_non_gtme_passes_without_t3_body():
    p = _valid_payload()
    # t3_body absent — valid for 2-touch families
    passed, reason = qa_check(p, _tokens(), sequence_family="ENT_REVMKTG_HIRING")
    assert passed, reason
