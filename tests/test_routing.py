"""
Tests for routing/assignment.py

Routing determines which sequence family a contact enters.
Wrong routing sends the wrong campaign to the wrong persona.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from routing.assignment import (
    assign_sequence_family,
    detect_trigger,
    infer_persona,
    select_narrative_pillar,
)


# ── infer_persona ─────────────────────────────────────────────────────────────

def test_infer_persona_revmktg():
    assert infer_persona("VP Revenue Marketing") == "revmktg_vp"
    assert infer_persona("Director of Demand Generation") == "revmktg_vp"
    assert infer_persona("Head of Demand Gen") == "revmktg_vp"


def test_infer_persona_abm():
    assert infer_persona("ABM Manager") == "abm_leader"
    assert infer_persona("Account-Based Marketing Lead") == "abm_leader"


def test_infer_persona_revops():
    assert infer_persona("VP Revenue Operations") == "revops"
    assert infer_persona("Marketing Operations Manager") == "revops"


def test_infer_persona_cmo():
    assert infer_persona("Chief Marketing Officer") == "cmo"
    assert infer_persona("CMO") == "cmo"


def test_infer_persona_gtm_engineer():
    assert infer_persona("GTM Engineer") == "gtm_engineer"
    assert infer_persona("GTM Engineering Lead") == "gtm_engineer"


def test_infer_persona_practitioner_fallback():
    assert infer_persona("Marketing Coordinator") == "practitioner"
    assert infer_persona("Product Manager") == "practitioner"
    assert infer_persona("Account Executive") == "practitioner"


def test_infer_persona_case_insensitive():
    assert infer_persona("vp revenue marketing") == "revmktg_vp"
    assert infer_persona("CMO") == "cmo"


# ── assign_sequence_family ────────────────────────────────────────────────────

def test_ent_revmktg_intent():
    assert assign_sequence_family("ENT", "revmktg_vp", "intent") == "ENT_REVMKTG_INTENT"


def test_ent_revmktg_hiring():
    assert assign_sequence_family("ENT", "revmktg_vp", "hiring") == "ENT_REVMKTG_HIRING"


def test_ent_abm_intent():
    assert assign_sequence_family("ENT", "abm_leader", "intent") == "ENT_ABM_INTENT"


def test_mm_revmktg_no_trigger():
    assert assign_sequence_family("MM", "revmktg_vp") == "MM_REVMKTG_TIMING"


def test_mm_revops_intent():
    assert assign_sequence_family("MM", "revops", "intent") == "MM_REVOPS_INTENT"


def test_mm_cmo():
    assert assign_sequence_family("MM", "cmo") == "MM_CMO_EXEC"


def test_gtme_engineer():
    assert assign_sequence_family("GTME", "gtm_engineer") == "GTME_VIBE_MKT"


def test_fallback_ent_unrecognized_persona():
    # ENT with unknown persona → ENT fallback
    result = assign_sequence_family("ENT", "practitioner")
    assert result == "ENT_REVMKTG_HIRING"


def test_fallback_mm_unrecognized_persona():
    # MM with unknown persona → default fallback
    result = assign_sequence_family("MM", "practitioner")
    assert result == "MM_REVMKTG_TIMING"


def test_no_fallback_returns_none():
    result = assign_sequence_family("ENT", "practitioner", use_fallback=False)
    assert result is None


def test_trigger_mismatch_falls_to_next_rule():
    # ENT revmktg_vp + None trigger → no exact match → fallback
    result = assign_sequence_family("ENT", "revmktg_vp", trigger_type=None)
    assert result == "ENT_REVMKTG_HIRING"  # fallback for ENT


# ── detect_trigger ────────────────────────────────────────────────────────────

def test_detect_trigger_hiring():
    account = {"keywords": ["demand generation", "ABM", "SaaS"]}
    assert detect_trigger(account) == "hiring"


def test_detect_trigger_none_when_no_signals():
    account = {"keywords": ["enterprise software", "cloud"]}
    assert detect_trigger(account) is None


def test_detect_trigger_none_empty():
    assert detect_trigger({}) is None


# ── select_narrative_pillar ───────────────────────────────────────────────────

def test_practitioner_always_simple():
    assert select_narrative_pillar("practitioner", touch_number=1) == "SIMPLE"
    assert select_narrative_pillar("practitioner", touch_number=2) == "SIMPLE"
    assert select_narrative_pillar("revmktg_vp", touch_number=1) == "SIMPLE"


def test_cmo_first_touch_simple():
    assert select_narrative_pillar("cmo", touch_number=1) == "SIMPLE"


def test_cmo_second_touch_visionary():
    assert select_narrative_pillar("cmo", touch_number=2) == "VISIONARY"
    assert select_narrative_pillar("cmo", touch_number=3) == "VISIONARY"
