from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    APPROVED = "approved"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"
    AWAITING_REPLY = "awaiting_reply"
    RESUME_SENT = "resume_sent"
    PAUSED = "paused"
    ERROR = "error"


class ApplicationAction(StrEnum):
    IMMEDIATE_CONTACT = "immediate_contact"
    SEND_RESUME = "send_resume"


class LlmJobDecisionValue(StrEnum):
    APPLY = "apply"
    SKIP = "skip"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class JobCard:
    platform_job_id: str
    title: str
    company: str
    source_url: str
    detail_url: str | None = None
    salary: str | None = None
    location: str | None = None
    raw_text: str | None = None


@dataclass(frozen=True)
class LlmJobDecision:
    decision: LlmJobDecisionValue
    confidence: float
    reason: str
    resume_match_signals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    raw_response: str | None = None


@dataclass(frozen=True)
class LlmReplyDecision:
    send_resume: bool
    reply_type: str
    reason: str
    needs_human: bool
    raw_response: str | None = None


@dataclass(frozen=True)
class RecruiterReply:
    conversation_id: str
    text: str
    sender: str
    is_system: bool = False
    job_id: str | None = None


@dataclass(frozen=True)
class PageRisk:
    reason: str
    evidence: str


@dataclass(frozen=True)
class RunSummary:
    discovered: int = 0
    duplicates: int = 0
    approved: int = 0
    skipped: int = 0
    needs_review: int = 0
    contacted: int = 0
    paused: int = 0
    dry_run: bool = True

