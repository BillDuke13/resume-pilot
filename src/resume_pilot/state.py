from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from resume_pilot.config import DEFAULT_DAILY_CAP, DEFAULT_TIMEZONE, ensure_private_parent
from resume_pilot.models import (
    ApplicationAction,
    JobCard,
    JobStatus,
    LlmJobDecision,
    LlmJobDecisionValue,
    LlmReplyDecision,
)

# A contact reservation is held only for the few seconds of the click flow. One
# left behind longer than this is from a run that died mid-click: it no longer
# counts toward the daily cap and may be cleared so the job can be retried, while
# a still-recent reservation belongs to a live run and must be left intact.
RESERVATION_TTL_SECONDS = 600


class StateError(Exception):
    """Base class for state-store failures."""


class BudgetExceededError(StateError):
    """Raised when an action would exceed the configured daily cap."""


class DuplicateActionError(StateError):
    """Raised when an action was already recorded for a job."""


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def isoformat_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def action_date(value: datetime | None = None, timezone: str = DEFAULT_TIMEZONE) -> str:
    return (value or utc_now()).astimezone(ZoneInfo(timezone)).date().isoformat()


class StateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        ensure_private_parent(self.path)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_job_id TEXT NOT NULL UNIQUE,
                    source_url TEXT NOT NULL,
                    detail_url TEXT,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    salary TEXT,
                    location TEXT,
                    raw_text TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    decision TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    resume_match_signals TEXT NOT NULL,
                    risk_flags TEXT NOT NULL,
                    raw_response TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS application_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    action_date TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_application_actions_real_unique
                    ON application_actions(job_id, action)
                    WHERE dry_run = 0;

                CREATE TABLE IF NOT EXISTS action_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reply_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                    conversation_id TEXT,
                    last_reply_text TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reply_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    send_resume INTEGER NOT NULL,
                    reply_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    needs_human INTEGER NOT NULL,
                    raw_response TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pauses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                """
            )
            connection.commit()
        self.path.chmod(0o600)

    def upsert_job(self, job: JobCard) -> tuple[int, bool]:
        now = isoformat_utc()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM jobs WHERE platform_job_id = ?",
                (job.platform_job_id,),
            ).fetchone()
            if row:
                connection.execute(
                    """
                    UPDATE jobs
                    SET source_url = ?, detail_url = COALESCE(?, detail_url),
                        title = ?, company = ?, salary = ?, location = ?,
                        raw_text = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        job.source_url,
                        job.detail_url,
                        job.title,
                        job.company,
                        job.salary,
                        job.location,
                        job.raw_text,
                        now,
                        row["id"],
                    ),
                )
                connection.commit()
                return int(row["id"]), False

            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    platform_job_id, source_url, detail_url, title, company, salary,
                    location, raw_text, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.platform_job_id,
                    job.source_url,
                    job.detail_url,
                    job.title,
                    job.company,
                    job.salary,
                    job.location,
                    job.raw_text,
                    JobStatus.DISCOVERED.value,
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid), True

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def get_job_by_platform_id(self, platform_job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM jobs WHERE platform_job_id = ?",
                (platform_job_id,),
            ).fetchone()

    def record_job_decision(self, job_id: int, decision: LlmJobDecision) -> None:
        status = {
            LlmJobDecisionValue.APPLY: JobStatus.APPROVED,
            LlmJobDecisionValue.SKIP: JobStatus.SKIPPED,
            LlmJobDecisionValue.NEEDS_REVIEW: JobStatus.NEEDS_REVIEW,
        }[decision.decision]
        now = isoformat_utc()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_decisions (
                    job_id, decision, confidence, reason, resume_match_signals,
                    risk_flags, raw_response, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    decision.decision.value,
                    decision.confidence,
                    decision.reason,
                    json.dumps(decision.resume_match_signals, ensure_ascii=False),
                    json.dumps(decision.risk_flags, ensure_ascii=False),
                    decision.raw_response,
                    now,
                ),
            )
            current = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            protected = (JobStatus.AWAITING_REPLY.value, JobStatus.RESUME_SENT.value)
            if current is None or current["status"] not in protected:
                connection.execute(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, now, job_id),
                )
            connection.commit()

    def action_count(
        self,
        action: ApplicationAction,
        *,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> int:
        day = action_date(when, timezone)
        active_since = isoformat_utc(
            (when or utc_now()) - timedelta(seconds=RESERVATION_TTL_SECONDS)
        )
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM application_actions
                WHERE action = ? AND action_date = ? AND dry_run = 0
                  AND (
                      COALESCE(json_extract(details, '$.reserved'), 0) = 0
                      OR created_at >= ?
                      OR EXISTS (
                          SELECT 1 FROM action_attempts
                          WHERE action_attempts.job_id = application_actions.job_id
                            AND action_attempts.action = application_actions.action
                            AND action_attempts.status IN ('started', 'clicked')
                      )
                  )
                """,
                (action.value, day, active_since),
            ).fetchone()
            return int(row["count"])

    def has_action(self, job_id: int, action: ApplicationAction) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM application_actions
                WHERE job_id = ? AND action = ? AND dry_run = 0
                  AND COALESCE(json_extract(details, '$.reserved'), 0) = 0
                LIMIT 1
                """,
                (job_id, action.value),
            ).fetchone()
            return row is not None

    def has_active_action_attempt(
        self, job_id: int, action: ApplicationAction, *, when: datetime | None = None
    ) -> bool:
        # Any attempt still in 'started'/'clicked' means a run crashed mid-contact:
        # the click may already have been dispatched, so it blocks indefinitely and
        # requires manual reconciliation. Only attempts explicitly finished — 'failed'
        # (a pre-click abort), 'recorded', or 'already_in_conversation' — are inactive.
        # (`when` is accepted for call-site compatibility but no longer used.)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM action_attempts
                WHERE job_id = ? AND action = ? AND status IN ('started', 'clicked')
                LIMIT 1
                """,
                (job_id, action.value),
            ).fetchone()
            return row is not None

    def start_action_attempt(
        self,
        job_id: int,
        action: ApplicationAction,
        *,
        details: dict[str, Any] | None = None,
    ) -> int:
        now = isoformat_utc()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO action_attempts (
                    job_id, action, status, details, created_at, updated_at
                )
                VALUES (?, ?, 'started', ?, ?, ?)
                """,
                (
                    job_id,
                    action.value,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_action_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE action_attempts
                SET status = ?, details = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    isoformat_utc(),
                    attempt_id,
                ),
            )
            connection.commit()

    def can_record_contact(
        self,
        *,
        daily_cap: int = DEFAULT_DAILY_CAP,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> bool:
        return (
            self.action_count(ApplicationAction.IMMEDIATE_CONTACT, when=when, timezone=timezone)
            < daily_cap
        )

    def record_contact(
        self,
        job_id: int,
        *,
        daily_cap: int = DEFAULT_DAILY_CAP,
        dry_run: bool = False,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._record_action(
            job_id,
            ApplicationAction.IMMEDIATE_CONTACT,
            next_status=JobStatus.AWAITING_REPLY,
            daily_cap=daily_cap,
            dry_run=dry_run,
            when=when,
            timezone=timezone,
            details=details or {},
        )
        if not dry_run:
            self._upsert_reply_queue(job_id, status=JobStatus.AWAITING_REPLY.value)

    def reserve_contact(
        self,
        job_id: int,
        *,
        daily_cap: int = DEFAULT_DAILY_CAP,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Atomically claim today's contact budget before any real click.

        Inserts the immediate-contact row inside one IMMEDIATE transaction that
        also enforces the daily cap and duplicate guard, so two concurrent runners
        cannot both pass a read-only cap check and then both click. The job status
        and reply queue are advanced only by confirm_contact() after the click
        succeeds; release_contact() removes the reservation if the click fails.
        """
        timestamp = when or utc_now()
        day = action_date(timestamp, timezone)
        now = isoformat_utc(timestamp)
        active_since = isoformat_utc(timestamp - timedelta(seconds=RESERVATION_TTL_SECONDS))
        payload = {**(details or {}), "reserved": True}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id FROM application_actions
                WHERE job_id = ? AND action = ? AND dry_run = 0
                  AND (
                      COALESCE(json_extract(details, '$.reserved'), 0) = 0
                      OR created_at >= ?
                  )
                """,
                (job_id, ApplicationAction.IMMEDIATE_CONTACT.value, active_since),
            ).fetchone()
            if existing:
                raise DuplicateActionError(
                    f"immediate_contact already recorded for job {job_id}"
                )
            # Clear only a provably stale reservation (older than the click-flow TTL,
            # so from a run that died mid-click). The partial unique index allows one
            # dry_run=0 row per job, so the stale row must go before the INSERT below;
            # a still-active reservation is left intact and blocks via the check above.
            connection.execute(
                """
                DELETE FROM application_actions
                WHERE job_id = ? AND action = ? AND dry_run = 0
                  AND COALESCE(json_extract(details, '$.reserved'), 0) = 1
                  AND created_at < ?
                """,
                (job_id, ApplicationAction.IMMEDIATE_CONTACT.value, active_since),
            )
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM application_actions
                WHERE action = ? AND action_date = ? AND dry_run = 0
                  AND (
                      COALESCE(json_extract(details, '$.reserved'), 0) = 0
                      OR created_at >= ?
                  )
                """,
                (ApplicationAction.IMMEDIATE_CONTACT.value, day, active_since),
            ).fetchone()
            if int(row["count"]) >= daily_cap:
                raise BudgetExceededError(
                    f"Daily cap {daily_cap} reached for immediate_contact on {day}"
                )
            connection.execute(
                """
                INSERT INTO application_actions (
                    job_id, action, action_date, dry_run, details, created_at
                )
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (
                    job_id,
                    ApplicationAction.IMMEDIATE_CONTACT.value,
                    day,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()

    def confirm_contact(
        self,
        job_id: int,
        *,
        when: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Finalize a reserved contact after a successful click."""
        now = isoformat_utc(when)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE application_actions
                SET details = ?
                WHERE job_id = ? AND action = ? AND dry_run = 0
                """,
                (
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    job_id,
                    ApplicationAction.IMMEDIATE_CONTACT.value,
                ),
            )
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.AWAITING_REPLY.value, now, job_id),
            )
            connection.commit()
        self._upsert_reply_queue(job_id, status=JobStatus.AWAITING_REPLY.value)

    def release_contact(self, job_id: int) -> None:
        """Release a contact reservation when the click did not happen."""
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM application_actions
                WHERE job_id = ? AND action = ? AND dry_run = 0
                """,
                (job_id, ApplicationAction.IMMEDIATE_CONTACT.value),
            )
            connection.commit()

    def record_resume_sent(
        self,
        job_id: int,
        *,
        dry_run: bool = False,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._record_action(
            job_id,
            ApplicationAction.SEND_RESUME,
            next_status=JobStatus.RESUME_SENT,
            dry_run=dry_run,
            when=when,
            timezone=timezone,
            details=details or {},
        )

    def _record_action(
        self,
        job_id: int,
        action: ApplicationAction,
        *,
        next_status: JobStatus,
        daily_cap: int | None = None,
        dry_run: bool = False,
        when: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        details: dict[str, Any],
    ) -> None:
        timestamp = when or utc_now()
        day = action_date(timestamp, timezone)
        now = isoformat_utc(timestamp)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id FROM application_actions
                WHERE job_id = ? AND action = ? AND dry_run = 0
                """,
                (job_id, action.value),
            ).fetchone()
            if existing and not dry_run:
                raise DuplicateActionError(f"{action.value} already recorded for job {job_id}")

            if not dry_run and daily_cap is not None:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM application_actions
                    WHERE action = ? AND action_date = ? AND dry_run = 0
                    """,
                    (action.value, day),
                ).fetchone()
                if int(row["count"]) >= daily_cap:
                    raise BudgetExceededError(
                        f"Daily cap {daily_cap} reached for {action.value} on {day}"
                    )

            connection.execute(
                """
                INSERT INTO application_actions (
                    job_id, action, action_date, dry_run, details, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    action.value,
                    day,
                    int(dry_run),
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            if not dry_run:
                connection.execute(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status.value, now, job_id),
                )
            connection.commit()

    def _upsert_reply_queue(
        self,
        job_id: int,
        *,
        status: str,
        conversation_id: str | None = None,
        last_reply_text: str | None = None,
    ) -> None:
        now = isoformat_utc()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reply_queue (
                    job_id, conversation_id, last_reply_text, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    conversation_id = COALESCE(
                        excluded.conversation_id,
                        reply_queue.conversation_id
                    ),
                    last_reply_text = COALESCE(
                        excluded.last_reply_text,
                        reply_queue.last_reply_text
                    ),
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (job_id, conversation_id, last_reply_text, status, now),
            )
            connection.commit()

    def record_reply_decision(self, job_id: int, decision: LlmReplyDecision) -> None:
        now = isoformat_utc()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reply_decisions (
                    job_id, send_resume, reply_type, reason, needs_human,
                    raw_response, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    int(decision.send_resume),
                    decision.reply_type,
                    decision.reason,
                    int(decision.needs_human),
                    decision.raw_response,
                    now,
                ),
            )
            connection.commit()

    def pause(self, reason: str, *, details: dict[str, Any] | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pauses (reason, details, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    reason,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    isoformat_utc(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def active_pauses(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM pauses WHERE resolved_at IS NULL ORDER BY created_at DESC"
                )
            )

    def resolve_pauses(self) -> int:
        """Mark every active pause resolved so the autonomous runner can resume.

        The startup gate refuses to run while any pause is unresolved; an operator
        calls this after handling the captcha/manual-verification in VNC.
        """
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE pauses SET resolved_at = ? WHERE resolved_at IS NULL",
                (isoformat_utc(),),
            )
            connection.commit()
            return int(cursor.rowcount)

    def recent_jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            )
