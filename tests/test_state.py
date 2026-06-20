from __future__ import annotations

import stat
from datetime import UTC, datetime

import pytest

from resume_pilot.models import ApplicationAction, JobCard, LlmJobDecision, LlmJobDecisionValue
from resume_pilot.state import BudgetExceededError, DuplicateActionError, StateStore


def make_job(platform_job_id: str = "job-1") -> JobCard:
    return JobCard(
        platform_job_id=platform_job_id,
        title="Python Automation Engineer",
        company="Example Tech",
        source_url="https://www.zhipin.com/web/geek/job",
        detail_url=f"https://www.zhipin.com/job_detail/{platform_job_id}.html",
        salary="30-45K",
        location="Beijing",
        raw_text="Python Automation Engineer Example Tech 30-45K Beijing",
    )


def make_decision(decision: LlmJobDecisionValue) -> LlmJobDecision:
    return LlmJobDecision(
        decision=decision,
        confidence=0.91,
        reason="Strong match",
        resume_match_signals=["Python", "browser automation"],
        risk_flags=[],
    )


def test_upsert_job_deduplicates_by_platform_id(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    first_id, inserted = store.upsert_job(make_job())
    second_id, second_inserted = store.upsert_job(make_job())

    assert inserted is True
    assert second_inserted is False
    assert first_id == second_id


def test_state_db_file_is_private(tmp_path):
    path = tmp_path / "state.sqlite"
    StateStore(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_record_decision_updates_status(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())

    store.record_job_decision(job_id, make_decision(LlmJobDecisionValue.APPLY))

    assert store.get_job(job_id)["status"] == "approved"


def test_contact_consumes_daily_budget_and_blocks_duplicates(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())
    now = datetime(2026, 6, 19, 1, 0, tzinfo=UTC)

    assert store.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT) is False

    store.record_contact(job_id, daily_cap=1, when=now)

    assert store.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT) is True

    with pytest.raises(DuplicateActionError):
        store.record_contact(job_id, daily_cap=1, when=now)

    second_id, _ = store.upsert_job(make_job("job-2"))
    with pytest.raises(BudgetExceededError):
        store.record_contact(second_id, daily_cap=1, when=now)


def test_dry_run_action_does_not_consume_budget_or_change_status(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())
    now = datetime(2026, 6, 19, 1, 0, tzinfo=UTC)

    store.record_contact(job_id, daily_cap=1, dry_run=True, when=now)

    assert store.action_count(store_action_immediate_contact(), when=now) == 0
    assert store.get_job(job_id)["status"] == "discovered"


def test_active_action_attempt_blocks_automatic_retry(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())

    attempt_id = store.start_action_attempt(
        job_id,
        ApplicationAction.IMMEDIATE_CONTACT,
        details={"source": "test"},
    )

    assert store.has_active_action_attempt(job_id, ApplicationAction.IMMEDIATE_CONTACT) is True

    store.finish_action_attempt(attempt_id, status="failed", details={"error": "before click"})

    assert store.has_active_action_attempt(job_id, ApplicationAction.IMMEDIATE_CONTACT) is False


def test_pause_records_active_pause(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    pause_id = store.pause("security_verification", details={"evidence": "captcha"})

    pauses = store.active_pauses()
    assert pause_id == pauses[0]["id"]
    assert pauses[0]["reason"] == "security_verification"


def store_action_immediate_contact():
    from resume_pilot.models import ApplicationAction

    return ApplicationAction.IMMEDIATE_CONTACT


def test_dry_run_action_does_not_block_a_later_real_contact(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())
    now = datetime(2026, 6, 19, 1, 0, tzinfo=UTC)

    store.record_contact(job_id, daily_cap=1, dry_run=True, when=now)
    store.record_contact(job_id, daily_cap=1, when=now)

    assert store.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT) is True
    assert store.action_count(ApplicationAction.IMMEDIATE_CONTACT, when=now) == 1


def test_record_job_decision_does_not_downgrade_contacted_job(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())
    store.record_contact(job_id, daily_cap=1)
    assert store.get_job(job_id)["status"] == "awaiting_reply"

    store.record_job_decision(job_id, make_decision(LlmJobDecisionValue.SKIP))

    assert store.get_job(job_id)["status"] == "awaiting_reply"


def test_reserve_contact_enforces_cap_atomically_and_can_release(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    job_id, _ = store.upsert_job(make_job())
    second_id, _ = store.upsert_job(make_job("job-2"))
    now = datetime(2026, 6, 19, 1, 0, tzinfo=UTC)

    store.reserve_contact(job_id, daily_cap=1, when=now)
    with pytest.raises(BudgetExceededError):
        store.reserve_contact(second_id, daily_cap=1, when=now)

    store.release_contact(job_id)
    assert store.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT) is False
    store.reserve_contact(second_id, daily_cap=1, when=now)
    assert store.has_action(second_id, ApplicationAction.IMMEDIATE_CONTACT) is True
