"""
Tests for CRM routing behavior when Smartlead enrollments are mirrored into Salesforce.
"""
import subprocess
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from connectors.crm_client import (
    CRMClient,
    OUTBOUND_LEAD_SOURCE,
    OUTBOUND_LEAD_SOURCE_DETAIL,
)


def test_run_sf_cmd_sets_autoupdate_disable(monkeypatch):
    """SF_AUTOUPDATE_DISABLE=true must be in the env passed to subprocess.run.

    This is the fix for the sf CLI exiting with code 2 when an update is
    available, which killed the pipeline. The env var suppresses the check.
    """
    captured = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"result":{"records":[]}}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = CRMClient()
    client._run_sf_query("SELECT Id FROM Account LIMIT 1")

    assert "SF_AUTOUPDATE_DISABLE" in captured["env"], (
        "SF_AUTOUPDATE_DISABLE must be passed to subprocess.run env — "
        "missing it causes the pipeline to die on sf CLI update warnings"
    )
    assert captured["env"]["SF_AUTOUPDATE_DISABLE"] == "true"


def test_run_sf_cmd_raises_on_non_zero_exit(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="AUTH_ERROR: session expired")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = CRMClient()

    try:
        client._run_sf_query("SELECT Id FROM Account LIMIT 1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "exit 1" in str(exc)
        assert "AUTH_ERROR" in str(exc)


def test_run_sf_cmd_raises_on_non_json_stdout(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="<html>rate limited</html>", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = CRMClient()

    try:
        client._run_sf_query("SELECT Id FROM Account LIMIT 1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "non-JSON" in str(exc)


def test_record_campaign_enrollment_returns_existing_task(monkeypatch):
    client = CRMClient()

    monkeypatch.setattr(
        client,
        "find_person_by_email",
        lambda email: {"Id": "003-test", "_sf_type": "Contact"},
    )
    monkeypatch.setattr(
        client,
        "_find_existing_task",
        lambda who_id, subject: {"Id": "00T-existing"},
    )

    result = client.record_campaign_enrollment(
        email="person@example.com",
        name="Pat Example",
        company="ExampleCo",
        internal_reference="assign-123",
        smartlead_campaign_id="smartlead-7",
        sequence_family="MM_CMO_EXEC",
    )

    assert result["action"] == "task_exists"
    assert result["record_id"] == "00T-existing"


def test_record_campaign_enrollment_creates_task_for_existing_person(monkeypatch):
    client = CRMClient()

    monkeypatch.setattr(
        client,
        "find_person_by_email",
        lambda email: {"Id": "003-test", "_sf_type": "Contact"},
    )
    monkeypatch.setattr(client, "_find_existing_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        client,
        "_run_sf_create",
        lambda sobject, values: {"id": "00T-created", "sobject": sobject, "values": values},
    )

    result = client.record_campaign_enrollment(
        email="person@example.com",
        name="Pat Example",
        company="ExampleCo",
        internal_reference="assign-123",
        smartlead_campaign_id="smartlead-7",
        sequence_family="MM_CMO_EXEC",
    )

    assert result["action"] == "task_created"
    assert result["record_id"] == "00T-created"


def test_record_campaign_enrollment_creates_lead_when_missing(monkeypatch):
    client = CRMClient()
    captured = {}

    monkeypatch.setattr(client, "find_person_by_email", lambda email: None)

    def fake_create(sobject, values):
        captured["sobject"] = sobject
        captured["values"] = values
        return {"id": "00Q-created", "sobject": sobject, "values": values}

    monkeypatch.setattr(client, "_run_sf_create", fake_create)

    result = client.record_campaign_enrollment(
        email="newperson@example.com",
        name="New Person",
        company="NewCo",
        internal_reference="assign-999",
        smartlead_campaign_id="smartlead-99",
        sequence_family="ENT_REVMKTG_INTENT",
    )

    assert result["action"] == "lead_created"
    assert result["record_id"] == "00Q-created"
    assert captured["sobject"] == "Lead"
    assert captured["values"]["LeadSource"] == OUTBOUND_LEAD_SOURCE
    assert captured["values"]["Lead_Source_Detail__c"] == OUTBOUND_LEAD_SOURCE_DETAIL


def test_sync_outbound_contact_creates_contact_when_account_exists(monkeypatch):
    client = CRMClient()

    monkeypatch.setattr(client, "find_person_by_email", lambda email: None)
    monkeypatch.setattr(client, "find_account_id", lambda **kwargs: "001-account")
    monkeypatch.setattr(
        client,
        "_run_sf_create",
        lambda sobject, values: {"id": "003-created", "sobject": sobject, "values": values},
    )

    result = client.sync_outbound_contact(
        {
            "first_name": "Pat",
            "last_name": "Example",
            "email": "pat@example.com",
            "title": "GTM Engineer",
            "department": "Marketing",
            "company_name": "ExampleCo",
            "domain": "example.com",
            "persona_type": "gtm_engineer",
            "source": "manual_gtme",
            "sequence_families": "GTME_VIBE_MKT",
        }
    )

    assert result["action"] == "contact_created"
    assert result["record_id"] == "003-created"
    assert result["account_id"] == "001-account"
    assert result["updated_fields"]


def test_sync_outbound_contact_updates_existing_contact(monkeypatch):
    client = CRMClient()

    contact = {
        "first_name": "Pat",
        "last_name": "Example",
        "email": "pat@example.com",
        "title": "Senior GTM Engineer",
        "department": "Marketing",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "persona_type": "gtm_engineer",
        "source": "manual_gtme",
        "sequence_families": "GTME_VIBE_MKT",
    }

    monkeypatch.setattr(client, "find_account_id", lambda **kwargs: "001-account")
    monkeypatch.setattr(
        client,
        "find_person_by_email",
        lambda email: {
            "Id": "003-existing",
            "_sf_type": "Contact",
            "FirstName": "Pat",
            "LastName": "Example",
            "Email": "pat@example.com",
            "Title": "GTM Engineer",
            "Department": "",
            "LeadSource": "",
            "Lead_Source_Detail__c": "",
            "Description": "",
            "AccountId": "",
        },
    )

    captured = {}

    def fake_update(sobject, record_id, values):
        captured["sobject"] = sobject
        captured["record_id"] = record_id
        captured["values"] = values
        return {"success": True}

    monkeypatch.setattr(client, "_run_sf_update", fake_update)

    result = client.sync_outbound_contact(contact)

    assert result["action"] == "contact_updated"
    assert result["record_id"] == "003-existing"
    assert captured["sobject"] == "Contact"
    assert captured["record_id"] == "003-existing"
    assert captured["values"]["Title"] == "Senior GTM Engineer"
    assert captured["values"]["Department"] == "Marketing"
    assert captured["values"]["AccountId"] == "001-account"
    assert captured["values"]["LeadSource"] == OUTBOUND_LEAD_SOURCE
    assert captured["values"]["Lead_Source_Detail__c"] == OUTBOUND_LEAD_SOURCE_DETAIL


def test_sync_outbound_contact_returns_unchanged_when_contact_matches(monkeypatch):
    client = CRMClient()

    contact = {
        "first_name": "Pat",
        "last_name": "Example",
        "email": "pat@example.com",
        "title": "Senior GTM Engineer",
        "department": "Marketing",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "persona_type": "gtm_engineer",
        "source": "manual_gtme",
        "sequence_families": "GTME_VIBE_MKT",
    }

    desired = client._build_contact_sync_values(contact, "001-account")

    monkeypatch.setattr(client, "find_account_id", lambda **kwargs: "001-account")
    monkeypatch.setattr(
        client,
        "find_person_by_email",
        lambda email: {"Id": "003-existing", "_sf_type": "Contact", **desired},
    )
    monkeypatch.setattr(
        client,
        "_run_sf_update",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected update")),
    )

    result = client.sync_outbound_contact(contact)

    assert result["action"] == "contact_unchanged"
    assert result["updated_fields"] == []


def test_sync_outbound_contact_updates_existing_lead(monkeypatch):
    client = CRMClient()

    contact = {
        "first_name": "Pat",
        "last_name": "Example",
        "email": "pat@example.com",
        "title": "Senior GTM Engineer",
        "company_name": "ExampleCo",
        "domain": "example.com",
        "persona_type": "gtm_engineer",
        "source": "manual_gtme",
        "sequence_families": "GTME_VIBE_MKT",
    }

    monkeypatch.setattr(client, "find_account_id", lambda **kwargs: None)
    monkeypatch.setattr(
        client,
        "find_person_by_email",
        lambda email: {
            "Id": "00Q-existing",
            "_sf_type": "Lead",
            "FirstName": "Pat",
            "LastName": "Example",
            "Company": "OldCo",
            "Email": "pat@example.com",
            "Title": "GTM Engineer",
            "LeadSource": "",
            "Lead_Source_Detail__c": "",
            "Description": "",
        },
    )

    captured = {}

    def fake_update(sobject, record_id, values):
        captured["sobject"] = sobject
        captured["record_id"] = record_id
        captured["values"] = values
        return {"success": True}

    monkeypatch.setattr(client, "_run_sf_update", fake_update)

    result = client.sync_outbound_contact(contact)

    assert result["action"] == "lead_updated"
    assert captured["sobject"] == "Lead"
    assert captured["record_id"] == "00Q-existing"
    assert captured["values"]["Company"] == "ExampleCo"
    assert captured["values"]["Title"] == "Senior GTM Engineer"
    assert captured["values"]["LeadSource"] == OUTBOUND_LEAD_SOURCE
    assert captured["values"]["Lead_Source_Detail__c"] == OUTBOUND_LEAD_SOURCE_DETAIL
