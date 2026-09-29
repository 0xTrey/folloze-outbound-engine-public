from pathlib import Path

from personalization.case_study_selector import CATALOG
from personalization.generator import generate_payload


ROOT = Path(__file__).resolve().parents[1]


def test_private_reference_package_is_not_committed():
    assert not (ROOT / "docs" / "messaging").exists()
    assert not (ROOT / ".agents" / "product-marketing-context.md").exists()


def test_public_engine_has_no_embedded_proof_catalog():
    assert CATALOG == []


def test_copy_generation_fails_closed_without_private_context():
    result = generate_payload(
        contact={"first_name": "Alex", "title": "Marketing Leader"},
        account={"company_name": "Example Co"},
        sequence_family="MM_REVMKTG_TIMING",
    )

    assert result["qa_status"] == "failed"
    assert "private messaging context is not configured" in result["qa_failure_reason"]


def test_live_destination_is_not_hard_coded():
    source = (ROOT / "jobs" / "assign_push.py").read_text(encoding="utf-8")
    assert "engage.folloze.com" not in source
