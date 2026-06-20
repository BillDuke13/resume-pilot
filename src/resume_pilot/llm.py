from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from resume_pilot.config import DEFAULT_CLAUDE_MODEL
from resume_pilot.models import LlmJobDecision, LlmJobDecisionValue, LlmReplyDecision


class LlmError(Exception):
    """Base class for LLM integration failures."""


class InvalidLlmResponseError(LlmError):
    """Raised when model output does not match the expected contract."""


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise InvalidLlmResponseError("Expected a JSON object")


def _extract_json_candidate(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.S)
    if fenced:
        return fenced.group(1)

    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        return stripped[first : last + 1]

    raise InvalidLlmResponseError("No JSON object found in model output")


def parse_json_payload(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_extract_json_candidate(raw_output))
    except json.JSONDecodeError as exc:
        raise InvalidLlmResponseError(f"Invalid JSON output: {exc}") from exc

    envelope = _coerce_json_object(parsed)
    for key in ("result", "content", "completion", "message"):
        value = envelope.get(key)
        if isinstance(value, str) and "{" in value:
            return parse_json_payload(value)
        if isinstance(value, list):
            joined = "\n".join(
                item.get("text", "") for item in value if isinstance(item, dict)
            )
            if "{" in joined:
                return parse_json_payload(joined)
    return envelope


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidLlmResponseError(f"Field {key!r} must be a non-empty string")
    return value.strip()


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise InvalidLlmResponseError(f"Field {key!r} must be a boolean")
    return value


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidLlmResponseError(f"Field {key!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def parse_job_decision(raw_output: str) -> LlmJobDecision:
    payload = parse_json_payload(raw_output)
    decision_text = _require_string(payload, "decision")
    try:
        decision = LlmJobDecisionValue(decision_text)
    except ValueError as exc:
        raise InvalidLlmResponseError(f"Unsupported job decision: {decision_text}") from exc

    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or confidence < 0
        or confidence > 1
    ):
        raise InvalidLlmResponseError("Field 'confidence' must be a number from 0 to 1")

    return LlmJobDecision(
        decision=decision,
        confidence=float(confidence),
        reason=_require_string(payload, "reason"),
        resume_match_signals=_string_list(payload, "resume_match_signals"),
        risk_flags=_string_list(payload, "risk_flags"),
        raw_response=raw_output,
    )


def parse_reply_decision(raw_output: str) -> LlmReplyDecision:
    payload = parse_json_payload(raw_output)
    return LlmReplyDecision(
        send_resume=_require_bool(payload, "send_resume"),
        reply_type=_require_string(payload, "reply_type"),
        reason=_require_string(payload, "reason"),
        needs_human=_require_bool(payload, "needs_human"),
        raw_response=raw_output,
    )


@dataclass(frozen=True)
class ClaudeCodeClient:
    model: str = DEFAULT_CLAUDE_MODEL
    executable: str = "claude"
    timeout_seconds: int = 120

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def run_json(self, prompt: str) -> str:
        if not self.available():
            raise LlmError(f"{self.executable!r} was not found on PATH")
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "-p",
                    "--model",
                    self.model,
                    "--tools",
                    "",
                    "--disallowedTools",
                    "mcp__*",
                    "--no-session-persistence",
                    "--output-format",
                    "json",
                ],
                check=False,
                capture_output=True,
                text=True,
                input=prompt,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise LlmError(
                f"Claude Code timed out after {self.timeout_seconds} seconds"
            ) from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise LlmError(f"Claude Code exited with {result.returncode}: {stderr}")
        return result.stdout

    def probe_json_capability(self) -> dict[str, Any]:
        output = self.run_json(
            'Return exactly this JSON object and no prose: {"ok": true, "capability": "json"}.'
        )
        payload = parse_json_payload(output)
        if payload.get("ok") is not True:
            raise InvalidLlmResponseError("Claude Code JSON probe did not return ok=true")
        return payload


class StaticDecisionClient:
    def __init__(self, decisions: Sequence[LlmJobDecision]):
        self._decisions = list(decisions)
        self.calls = 0

    def decide_job(self, _prompt: str) -> LlmJobDecision:
        if self.calls >= len(self._decisions):
            raise LlmError("No static decision configured")
        decision = self._decisions[self.calls]
        self.calls += 1
        return decision


@dataclass(frozen=True)
class FixedJsonDecisionClient:
    decision: LlmJobDecisionValue
    confidence: float = 0.99
    reason: str = "Deterministic smoke-test decision."

    def run_json(self, _prompt: str) -> str:
        return json.dumps(
            {
                "decision": self.decision.value,
                "confidence": self.confidence,
                "reason": self.reason,
                "resume_match_signals": ["fixture"],
                "risk_flags": [],
            }
        )
