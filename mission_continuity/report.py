"""The agent's structured final report (Pydantic AI output_type)."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    title: str = Field(description="Your own short label for this finding")
    verdict: Literal["ACTUAL_ERROR", "APPARENT_NOT_ERROR", "UNRESOLVED"]
    amount_at_issue: Optional[float] = None
    record_ids: List[str] = Field(description="Record ids this finding rests on, e.g. TX-..., INV-..., CR-..., SE-..., U-..., SUP-...")
    explanation: str
    evidence_tool_call_ids: List[str] = Field(default_factory=list)


class Correction(BaseModel):
    prior_statement_ref: str = Field(description="Ticket id, or 'agent' for your own earlier hypothesis")
    prior_claim: str
    corrected_claim: str
    evidence_record_ids: List[str] = Field(default_factory=list)


class HumanAction(BaseModel):
    action_type: Literal["refund", "apply_credit", "withdraw_commitment",
                         "customer_communication", "ticket_update", "other"]
    description: str
    amount: Optional[float] = None
    related_record_ids: List[str] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    findings: List[Finding]
    corrections: List[Correction]
    unresolved_questions: List[str]
    actions_requiring_human_authorization: List[HumanAction]
    actions_taken_by_agent: List[str] = Field(
        description="Every action you actually performed with tools (not recommendations)")
