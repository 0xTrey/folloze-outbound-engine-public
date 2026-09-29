from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, field_validator


class Account(BaseModel):
    account_id: Optional[UUID] = None
    domain: str
    company_name: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    revenue_band: Optional[str] = None
    geography: Optional[str] = None
    fit_score: Optional[float] = None
    timing_score: Optional[float] = None
    priority_score: Optional[float] = None
    segment: Optional[str] = None        # ENT | MM | SMB
    owner_rep: Optional[str] = None
    crm_account_id: Optional[str] = None
    account_status: str = "active"
    apollo_raw: Optional[dict] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("account_status")
    @classmethod
    def valid_status(cls, v: str) -> str:
        allowed = {"active", "customer", "open_opp", "suppressed"}
        if v not in allowed:
            raise ValueError(f"account_status must be one of {allowed}")
        return v

    @field_validator("segment")
    @classmethod
    def valid_segment(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"ENT", "MM", "SMB"}:
            raise ValueError("segment must be ENT, MM, or SMB")
        return v


class Contact(BaseModel):
    contact_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    seniority: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    email_status: Optional[str] = None   # verified | risky | invalid | unknown
    linkedin_url: Optional[str] = None
    persona_type: Optional[str] = None   # revmktg_vp | revops | abm_leader | cmo | practitioner
    source: str = "apollo"
    last_enriched_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class PersonalizationPayload(BaseModel):
    payload_id: Optional[UUID] = None
    contact_id: UUID
    template_version: Optional[str] = None
    deterministic_tokens: Optional[dict] = None
    ai_opener: Optional[str] = None
    ai_why_now: Optional[str] = None
    ai_proof_point: Optional[str] = None
    ai_cta: Optional[str] = None
    model_used: Optional[str] = None
    prompt_version: Optional[str] = None
    qa_status: str = "pending"           # pending | approved | failed | superseded
    qa_failure_reason: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
