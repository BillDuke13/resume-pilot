from __future__ import annotations

import subprocess

import pytest

from resume_pilot.llm import (
    ClaudeCodeClient,
    FixedJsonDecisionClient,
    InvalidLlmResponseError,
    LlmError,
    parse_job_decision,
    parse_json_payload,
    parse_reply_decision,
)
from resume_pilot.models import LlmJobDecisionValue


def test_parse_json_payload_handles_claude_envelope():
    payload = parse_json_payload(
        '{"result": "```json\\n{\\"decision\\": \\"skip\\", \\"confidence\\": 0.7, '
        '\\"reason\\": \\"Mismatch\\", \\"resume_match_signals\\": [], '
        '\\"risk_flags\\": [\\"low salary\\"]}\\n```"}'
    )

    assert payload["decision"] == "skip"
    assert payload["risk_flags"] == ["low salary"]


def test_parse_job_decision_accepts_expected_schema():
    decision = parse_job_decision(
        """
        {
          "decision": "apply",
          "confidence": 0.88,
          "reason": "Matches Python automation experience",
          "resume_match_signals": ["Python", "Playwright"],
          "risk_flags": []
        }
        """
    )

    assert decision.decision is LlmJobDecisionValue.APPLY
    assert decision.confidence == 0.88


def test_parse_job_decision_rejects_unknown_decision():
    with pytest.raises(InvalidLlmResponseError):
        parse_job_decision(
            """
            {
              "decision": "maybe",
              "confidence": 0.5,
              "reason": "Unknown",
              "resume_match_signals": [],
              "risk_flags": []
            }
            """
        )


def test_parse_reply_decision_accepts_expected_schema():
    decision = parse_reply_decision(
        """
        {
          "send_resume": true,
          "reply_type": "recruiter_interested",
          "reason": "Recruiter asked for resume",
          "needs_human": false
        }
        """
    )

    assert decision.send_resume is True
    assert decision.needs_human is False


def test_fixed_json_decision_client_produces_parseable_job_decision():
    client = FixedJsonDecisionClient(LlmJobDecisionValue.NEEDS_REVIEW)

    decision = parse_job_decision(client.run_json("ignored"))

    assert decision.decision is LlmJobDecisionValue.NEEDS_REVIEW
    assert decision.resume_match_signals == ["fixture"]


def test_claude_code_timeout_error_does_not_include_prompt(monkeypatch):
    secret_prompt = "private resume profile should not appear in errors"
    client = ClaudeCodeClient(timeout_seconds=3)

    monkeypatch.setattr(ClaudeCodeClient, "available", lambda _self: True)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["claude", "-p", secret_prompt],
            timeout=3,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LlmError) as exc_info:
        client.run_json(secret_prompt)

    message = str(exc_info.value)
    assert "timed out after 3 seconds" in message
    assert secret_prompt not in message
