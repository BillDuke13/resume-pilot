from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from resume_pilot.boss import BossHtmlAdapter, HumanPauseRequired
from resume_pilot.config import DEFAULT_DAILY_CAP, DEFAULT_TIMEZONE
from resume_pilot.llm import ClaudeCodeClient, InvalidLlmResponseError, parse_job_decision
from resume_pilot.models import (
    ApplicationAction,
    JobCard,
    LlmJobDecision,
    LlmJobDecisionValue,
    RunSummary,
)
from resume_pilot.state import StateStore

MIN_APPLY_CONFIDENCE = 0.75
MAX_JOB_PROMPT_TEXT_CHARS = 6000


@dataclass(frozen=True)
class SalaryRangeK:
    minimum: int
    maximum: int | None = None


def parse_monthly_salary_range_k(value: str | None) -> SalaryRangeK | None:
    if not value:
        return None
    match = re.search(
        r"(?P<minimum>\d{1,3})(?:\s*-\s*(?P<maximum>\d{1,3}))?\s*K",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    maximum = match.group("maximum")
    return SalaryRangeK(
        minimum=int(match.group("minimum")),
        maximum=int(maximum) if maximum is not None else None,
    )


def salary_meets_candidate_floor(
    salary: str | None,
    *,
    minimum_monthly_salary_k: int,
) -> bool:
    parsed = parse_monthly_salary_range_k(salary)
    return parsed is not None and parsed.minimum >= minimum_monthly_salary_k


class ContactExecutor(Protocol):
    def __call__(self, job: JobCard) -> dict[str, Any]: ...


def build_job_decision_prompt(job: JobCard, profile_summary: str | None = None) -> str:
    profile = profile_summary or "No resume profile summary has been extracted yet."
    raw_text = (job.raw_text or "")[:MAX_JOB_PROMPT_TEXT_CHARS]
    return f"""
You decide whether this BOSS Zhipin role should receive the first platform action.
Return only JSON with fields:
decision: "apply" | "skip" | "needs_review"
confidence: number from 0 to 1
reason: short string
resume_match_signals: array of strings
risk_flags: array of strings

Safety rules:
- Do not recommend actions that bypass verification, captchas, rate limits, or login gates.
- Use "needs_review" if page evidence is ambiguous.
- "apply" only means click the platform's visible immediate-contact control.
- Treat explicit candidate-side policy included in the resume profile as authoritative.
- Distinguish candidate-side hard requirements from employer-side qualifications; when
  private policy says to ignore an employer-side requirement, apply that instruction in
  your final decision.
- Do not invent missing candidate requirements. If required policy evidence is absent or
  ambiguous, return "needs_review".

Resume profile:
{profile}

Job:
title: {job.title}
company: {job.company}
salary: {job.salary or ""}
location: {job.location or ""}
detail_url: {job.detail_url or ""}
raw_text: {raw_text}
""".strip()


@dataclass
class ResumePilotRunner:
    state: StateStore
    llm_client: ClaudeCodeClient
    adapter: BossHtmlAdapter
    timezone: str = DEFAULT_TIMEZONE

    def evaluate_static_html(
        self,
        html: str,
        *,
        source_url: str,
        dry_run: bool = True,
        daily_cap: int = DEFAULT_DAILY_CAP,
        limit: int | None = None,
        profile_summary: str | None = None,
        contact_executor: ContactExecutor | None = None,
        minimum_monthly_salary_k: int | None = None,
    ) -> RunSummary:
        if not dry_run and contact_executor is None:
            raise HumanPauseRequired(
                "execute_requires_live_contact_executor",
                {"source_url": source_url, "daily_cap": daily_cap},
            )

        risks = self.adapter.page_risks(html)
        if risks:
            self.state.pause(
                "page_risk_before_run",
                details={"risks": [risk.__dict__ for risk in risks], "source_url": source_url},
            )
            raise HumanPauseRequired("page_risk_before_run", {"risks": risks})

        jobs = self.adapter.extract_job_cards(html, source_url=source_url)
        if not dry_run and len(jobs) != 1:
            self.state.pause(
                "live_execute_requires_single_job",
                details={"job_count": len(jobs), "source_url": source_url},
            )
            raise HumanPauseRequired(
                "live_execute_requires_single_job",
                {"job_count": len(jobs), "source_url": source_url},
            )
        if limit is not None:
            jobs = jobs[:limit]

        summary = RunSummary(discovered=len(jobs), dry_run=dry_run)
        for job in jobs:
            job_id, inserted = self.state.upsert_job(job)
            if not inserted:
                summary = _replace_summary(summary, duplicates=summary.duplicates + 1)

            if minimum_monthly_salary_k is not None:
                salary_gate_decision = self._salary_gate_decision(
                    job,
                    minimum_monthly_salary_k=minimum_monthly_salary_k,
                )
                if salary_gate_decision:
                    self.state.record_job_decision(job_id, salary_gate_decision)
                    summary = _record_decision_summary(summary, salary_gate_decision)
                    continue

            decision = self._decide(job, profile_summary=profile_summary)
            self.state.record_job_decision(job_id, decision)
            summary = _record_decision_summary(summary, decision)

            if decision.decision != LlmJobDecisionValue.APPLY:
                continue

            if dry_run:
                continue

            if decision.confidence < MIN_APPLY_CONFIDENCE or decision.risk_flags:
                self.state.pause(
                    "apply_decision_not_safe",
                    details={
                        "job_id": job.platform_job_id,
                        "confidence": decision.confidence,
                        "risk_flags": decision.risk_flags,
                    },
                )
                raise HumanPauseRequired(
                    "apply_decision_not_safe",
                    {
                        "job_id": job.platform_job_id,
                        "confidence": decision.confidence,
                        "risk_flags": decision.risk_flags,
                    },
                )

            if self.state.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT):
                self.state.pause(
                    "duplicate_contact_action",
                    details={"job_id": job.platform_job_id},
                )
                raise HumanPauseRequired(
                    "duplicate_contact_action",
                    {"job_id": job.platform_job_id},
                )
            if not self.state.can_record_contact(
                daily_cap=daily_cap,
                timezone=self.timezone,
            ):
                self.state.pause(
                    "daily_contact_cap_reached",
                    details={"daily_cap": daily_cap, "job_id": job.platform_job_id},
                )
                raise HumanPauseRequired(
                    "daily_contact_cap_reached",
                    {"daily_cap": daily_cap, "job_id": job.platform_job_id},
                )

            if self.state.has_active_action_attempt(job_id, ApplicationAction.IMMEDIATE_CONTACT):
                self.state.pause(
                    "active_contact_attempt_exists",
                    details={"job_id": job.platform_job_id},
                )
                raise HumanPauseRequired(
                    "active_contact_attempt_exists",
                    {"job_id": job.platform_job_id},
                )

            can_click, click_risks = self.adapter.can_click_contact(html)
            if not can_click:
                self.state.pause(
                    "contact_button_not_safe",
                    details={
                        "risks": [risk.__dict__ for risk in click_risks],
                        "job_id": job.platform_job_id,
                    },
                )
                raise HumanPauseRequired("contact_button_not_safe", {"risks": click_risks})

            attempt_id = self.state.start_action_attempt(
                job_id,
                ApplicationAction.IMMEDIATE_CONTACT,
                details={"job_id": job.platform_job_id, "source_url": source_url},
            )
            try:
                details = contact_executor(job)
            except Exception as exc:
                self.state.finish_action_attempt(
                    attempt_id,
                    status="failed",
                    details={"job_id": job.platform_job_id, "error": str(exc)},
                )
                raise

            click_details = details | {"attempt_id": attempt_id}
            self.state.finish_action_attempt(
                attempt_id,
                status="clicked",
                details=click_details,
            )
            self.state.record_contact(
                job_id,
                daily_cap=daily_cap,
                dry_run=False,
                timezone=self.timezone,
                details=click_details,
            )
            self.state.finish_action_attempt(
                attempt_id,
                status="recorded",
                details=click_details,
            )
            summary = _replace_summary(summary, contacted=summary.contacted + 1)
            if click_details.get("needs_manual_verification"):
                self.state.pause(
                    "contact_click_needs_manual_verification",
                    details=click_details,
                )
                summary = _replace_summary(summary, paused=summary.paused + 1)
        return summary

    def evaluate_html_file(
        self,
        html_file: Path,
        *,
        source_url: str,
        dry_run: bool = True,
        daily_cap: int = DEFAULT_DAILY_CAP,
        limit: int | None = None,
        profile_summary: str | None = None,
    ) -> RunSummary:
        return self.evaluate_static_html(
            html_file.read_text(encoding="utf-8"),
            source_url=source_url,
            dry_run=dry_run,
            daily_cap=daily_cap,
            limit=limit,
            profile_summary=profile_summary,
        )

    def _salary_gate_decision(
        self,
        job: JobCard,
        *,
        minimum_monthly_salary_k: int,
    ) -> LlmJobDecision | None:
        parsed = parse_monthly_salary_range_k(job.salary)
        if parsed is None:
            return LlmJobDecision(
                decision=LlmJobDecisionValue.SKIP,
                confidence=1.0,
                reason=f"salary_missing_or_unparseable: requires >= {minimum_monthly_salary_k}K",
                resume_match_signals=[],
                risk_flags=[],
            )
        if parsed.minimum < minimum_monthly_salary_k:
            return LlmJobDecision(
                decision=LlmJobDecisionValue.SKIP,
                confidence=1.0,
                reason=f"salary_floor_below_candidate_floor:{parsed.minimum}K<{minimum_monthly_salary_k}K",
                resume_match_signals=[],
                risk_flags=[],
            )
        return None

    def _decide(self, job: JobCard, *, profile_summary: str | None = None) -> LlmJobDecision:
        prompt = build_job_decision_prompt(job, profile_summary)
        raw_output = self.llm_client.run_json(prompt)
        try:
            return parse_job_decision(raw_output)
        except InvalidLlmResponseError:
            raise


def _replace_summary(summary: RunSummary, **updates: int | bool) -> RunSummary:
    values = summary.__dict__ | updates
    return RunSummary(**values)


def _record_decision_summary(summary: RunSummary, decision: LlmJobDecision) -> RunSummary:
    if decision.decision == LlmJobDecisionValue.APPLY:
        return _replace_summary(summary, approved=summary.approved + 1)
    if decision.decision == LlmJobDecisionValue.SKIP:
        return _replace_summary(summary, skipped=summary.skipped + 1)
    return _replace_summary(summary, needs_review=summary.needs_review + 1)
