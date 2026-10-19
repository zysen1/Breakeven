"""Data models for cases, predictions, costs, and qualification targets.

Ground truth lives on ``Case``. Agents receive only ``CaseView`` so a shadow
run cannot read the label it is supposed to predict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Decision = Literal["approve", "deny", "request_docs"]
Action = Literal["approve", "deny", "request_docs", "pay", "close"]


class CaseView(BaseModel):
    """Features an agent is allowed to see."""

    case_id: str
    category: str
    amount: float = Field(ge=0)
    state: str
    documents_complete: bool
    fraud_signal: float = Field(ge=0, le=1)
    exception_flag: bool
    region: str
    prior_claims: int = Field(ge=0)
    claimant_tenure_years: float = Field(ge=0)

    def cache_payload(self) -> dict:
        return self.model_dump(mode="json")


class Case(CaseView):
    """A labeled historical case. ``decision_truth`` and ``fraud`` are hidden from agents."""

    decision_truth: Decision
    fraud: bool

    def view(self) -> CaseView:
        return CaseView(**self.model_dump())


class Prediction(BaseModel):
    case_id: str
    agent: str
    decision: Decision
    confidence: float = Field(ge=0, le=1)
    action: Action
    latency_ms: float = 0
    model_cost: float = 0
    cached: bool = False

    @field_validator("confidence")
    @classmethod
    def _clip_display(cls, value: float) -> float:
        if value != value:  # NaN
            raise ValueError("confidence must be a number")
        return float(value)


class CostModel(BaseModel):
    """Customer-supplied fully loaded cost parameters. Nothing here is an industry average."""

    currency: str = "USD"
    hourly_rate: float = Field(gt=0)
    hours_per_case_human: float = Field(gt=0)
    hours_per_review: float = Field(ge=0)
    hours_per_exception: float = Field(ge=0)
    hours_per_correction: float = Field(ge=0)
    hours_per_audit: float = Field(ge=0)
    hours_per_dual_review: float = Field(ge=0)
    maintenance_hours: float = Field(ge=0)
    human_maintenance_hours: float = Field(default=0, ge=0)
    model_cost_per_case: float = Field(default=0, ge=0)
    human_accuracy: float = Field(default=1.0, ge=0, le=1)


class Target(BaseModel):
    """Reliability target and the confidence level of the one-sided lower bound."""

    reliability: float = Field(gt=0, le=1)
    confidence: float = Field(default=0.95, gt=0, lt=1)
    human_accuracy: float = Field(default=1.0, ge=0, le=1)

    @property
    def alpha(self) -> float:
        return 1.0 - self.confidence
