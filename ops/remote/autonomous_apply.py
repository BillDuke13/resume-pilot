from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from resume_pilot.boss import (
    AMBIGUOUS_RESUME_MARKERS,
    LOGIN_MARKERS,
    POLICY_GATE_MARKERS,
    SECURITY_MARKERS,
    BossHtmlAdapter,
    normalize_text,
)
from resume_pilot.browser import LOOPBACK_CDP_HOSTS, BrowserManager
from resume_pilot.cdp import (
    CdpClickError,
    CdpError,
    cdp_bring_to_front,
    cdp_dispatch_mouse_click,
    cdp_evaluate,
    cdp_navigate,
    ensure_page_target,
    page_html,
    page_text,
    target_url_matches,
)
from resume_pilot.config import DEFAULT_CLAUDE_MODEL, DEFAULT_DAILY_CAP, AppPaths, default_cdp_url
from resume_pilot.llm import ClaudeCodeClient, InvalidLlmResponseError, LlmError, parse_job_decision
from resume_pilot.models import ApplicationAction, JobCard, LlmJobDecision, LlmJobDecisionValue
from resume_pilot.runner import build_job_decision_prompt, parse_monthly_salary_range_k
from resume_pilot.state import BudgetExceededError, DuplicateActionError, StateStore

POLICY_ENV = "RESUME_PILOT_AUTONOMOUS_POLICY"
SUCCESS_MARKERS = ("发送简历", "继续沟通", "沟通中")
PRE_CLICK_ABORT_PREFIXES = (
    "contact_button_unavailable:",
    "page_risk_before_click:",
    "contact_button_not_safe:",
)
JOB_DETAIL_PATH_RE = re.compile(r"/job_detail/[^/?#]+")
ALLOWED_BOSS_HOSTS = {
    "zhipin.com",
    "www.zhipin.com",
    "bosszhipin.com",
    "www.bosszhipin.com",
}


def is_allowed_boss_detail_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in ALLOWED_BOSS_HOSTS:
        return False
    return bool(JOB_DETAIL_PATH_RE.search(parsed.path))


adapter = BossHtmlAdapter()
paths = AppPaths.defaults()
store = StateStore(paths.state_db)
client = ClaudeCodeClient(
    model=os.environ.get("RESUME_PILOT_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
    timeout_seconds=180,
)


class PolicyError(RuntimeError):
    """Raised when the private autonomous policy is missing or invalid."""


@dataclass(frozen=True)
class AutonomousPolicy:
    daily_cap: int
    city: str | None
    salary_param: str | None
    minimum_monthly_salary_k: int | None
    search_keywords: list[str]
    role_include_terms: list[str]
    title_reject_terms: list[str]
    ignored_model_risk_patterns: list[re.Pattern[str]]
    profile_analysis_path: Path
    include_profile_keywords: bool
    llm_policy: dict[str, Any]

    @classmethod
    def load(cls, app_paths: AppPaths) -> AutonomousPolicy:
        policy_path = Path(
            os.environ.get(POLICY_ENV, app_paths.data_dir / "autonomous-policy.json")
        ).expanduser()
        if not policy_path.exists():
            raise PolicyError(f"Missing private policy file: {policy_path}")
        payload = _json_object(policy_path)

        search_keywords = _string_list(payload, "search_keywords")
        role_include_terms = _string_list(payload, "role_include_terms")
        if not search_keywords:
            raise PolicyError("Policy field 'search_keywords' must contain at least one item")

        ignored_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in _string_list(payload, "ignored_model_risk_patterns")
        ]
        profile_path = Path(
            payload.get("profile_analysis_path")
            or app_paths.data_dir / "profile-analysis.json"
        ).expanduser()

        llm_policy = payload.get("llm_policy", {})
        if llm_policy is None:
            llm_policy = {}
        if not isinstance(llm_policy, dict):
            raise PolicyError("Policy field 'llm_policy' must be an object when provided")

        return cls(
            daily_cap=_positive_int(payload.get("daily_cap", DEFAULT_DAILY_CAP), "daily_cap"),
            city=_optional_string(payload.get("city")),
            salary_param=_optional_string(payload.get("salary_param")),
            minimum_monthly_salary_k=_optional_positive_int(
                payload.get("minimum_monthly_salary_k"),
                "minimum_monthly_salary_k",
            ),
            search_keywords=search_keywords,
            role_include_terms=role_include_terms,
            title_reject_terms=_string_list(payload, "title_reject_terms"),
            ignored_model_risk_patterns=ignored_patterns,
            profile_analysis_path=profile_path,
            include_profile_keywords=bool(payload.get("include_profile_keywords", True)),
            llm_policy=llm_policy,
        )


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PolicyError(f"Policy file must contain a JSON object: {path}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("Expected a string or null policy value")
    return value.strip() or None


def _positive_int(value: Any, key: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise PolicyError(f"Policy field {key!r} must be a positive integer")
    return value


def _optional_positive_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, key)


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"Policy field {key!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def pause(reason: str, **details: Any) -> None:
    store.pause("autonomous_run_paused", details={"reason": reason, **details})
    emit("paused", reason=reason, **details)


def load_profile(policy: AutonomousPolicy) -> tuple[str, list[str]]:
    data: dict[str, Any] = {}
    if policy.profile_analysis_path.exists():
        raw_profile = json.loads(policy.profile_analysis_path.read_text(encoding="utf-8"))
        if isinstance(raw_profile, dict):
            data = raw_profile
    if isinstance(data.get("risk_flags_to_watch"), list):
        data["risk_flags_to_watch"] = [
            item
            for item in data["risk_flags_to_watch"]
            if not _matches_any_pattern(policy.ignored_model_risk_patterns, str(item))
        ]
    data["autonomous_policy"] = {
        "minimum_monthly_salary_k": policy.minimum_monthly_salary_k,
        "role_include_terms": policy.role_include_terms,
        "title_reject_terms": policy.title_reject_terms,
        "llm_policy": policy.llm_policy,
    }

    keywords = policy.search_keywords[:]
    profile_keywords = data.get("boss_search_keywords")
    if policy.include_profile_keywords and isinstance(profile_keywords, list):
        for item in profile_keywords:
            if isinstance(item, str) and item.strip() and item.strip() not in keywords:
                keywords.append(item.strip())
    return json.dumps(compact_profile_for_decision(data, policy), ensure_ascii=False), keywords


def compact_profile_for_decision(
    data: dict[str, Any],
    policy: AutonomousPolicy,
) -> dict[str, Any]:
    return {
        "candidate_positioning": _compact_profile_value(
            data.get("candidate_positioning"),
            policy,
        ),
        "seniority": _compact_profile_value(data.get("seniority"), policy),
        "years_experience": data.get("years_experience"),
        "core_strengths": _compact_profile_value(data.get("core_strengths"), policy),
        "target_roles": _compact_profile_value(data.get("target_roles"), policy),
        "acceptable_roles": _compact_profile_value(data.get("acceptable_roles"), policy),
        "avoid_roles": _compact_profile_value(data.get("avoid_roles"), policy),
        "salary_expectation": _compact_profile_value(data.get("salary_expectation"), policy),
        "hard_requirements": _compact_profile_value(data.get("hard_requirements"), policy),
        "strong_match_signals": _compact_profile_value(data.get("strong_match_signals"), policy),
        "risk_flags_to_watch": _compact_profile_value(data.get("risk_flags_to_watch"), policy),
        "autonomous_policy": data["autonomous_policy"],
    }


def _compact_profile_value(value: Any, policy: AutonomousPolicy) -> Any:
    if isinstance(value, str):
        if _matches_any_pattern(policy.ignored_model_risk_patterns, value):
            return None
        return value[:800]
    if isinstance(value, list):
        compacted = [
            _compact_profile_value(item, policy)
            for item in value
            if not _matches_any_pattern(policy.ignored_model_risk_patterns, str(item))
        ]
        return [item for item in compacted if item not in (None, {}, [])][:12]
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            if _matches_any_pattern(policy.ignored_model_risk_patterns, str(key)):
                continue
            compact_item = _compact_profile_value(item, policy)
            if compact_item not in (None, {}, []):
                compacted[str(key)] = compact_item
        return compacted
    return value


def search_url(keyword: str, policy: AutonomousPolicy) -> str:
    params = {"query": keyword}
    if policy.city:
        params["city"] = policy.city
    if policy.salary_param:
        params["salary"] = policy.salary_param
    return "https://www.zhipin.com/web/geek/jobs?" + urllib.parse.urlencode(params)


def visible_page_risk_payload(text: str) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    for marker in LOGIN_MARKERS:
        if marker in text:
            risks.append({"reason": "login_required_or_policy_gate", "evidence": marker})
    for marker in POLICY_GATE_MARKERS:
        if marker in text and "用户协议" in text and "隐私政策" in text:
            risks.append({"reason": "login_required_or_policy_gate", "evidence": marker})
    for marker in SECURITY_MARKERS:
        if marker in text:
            risks.append({"reason": "security_verification", "evidence": marker})
    for marker in AMBIGUOUS_RESUME_MARKERS:
        if marker in text:
            risks.append({"reason": "ambiguous_resume_selection", "evidence": marker})
    return risks


def html_to_text(html: str) -> str:
    return normalize_text(BeautifulSoup(html, "html.parser").get_text(" "))


def trim_detail_text_for_decision(text: str) -> str:
    prefix = text[:1500]
    body_start_candidates = [
        index
        for marker in ("职位描述", "岗位职责", "任职要求", "职位要求")
        if (index := text.find(marker)) >= 0
    ]
    body = text[min(body_start_candidates) :] if body_start_candidates else text

    end_candidates = [
        index
        for marker in (
            "公司介绍",
            "工商信息",
            "工作地址",
            "更多职位",
            "看过该职位的人还看了",
            "精选职位",
            "页面更新时间",
            "登录BOSS直聘",
            "短信登录",
            "扫码登录",
        )
        if (index := body.find(marker)) > 200
    ]
    if end_candidates:
        body = body[: min(end_candidates)]

    if body and body not in prefix:
        return f"{prefix}\n...\n{body}"[:6000]
    return (body or prefix)[:6000]


def _matches_any_pattern(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def salary_skip_reason(job: JobCard, policy: AutonomousPolicy) -> str | None:
    if policy.minimum_monthly_salary_k is None:
        return None
    parsed = parse_monthly_salary_range_k(job.salary)
    if parsed is None:
        return "salary_missing_or_unparseable"
    if parsed.minimum < policy.minimum_monthly_salary_k:
        return (
            "salary_floor_below_candidate_floor:"
            f"{parsed.minimum}K<{policy.minimum_monthly_salary_k}K"
        )
    return None


def record_skip(job_id: int, reason: str, confidence: float = 1.0) -> None:
    decision = LlmJobDecision(
        decision=LlmJobDecisionValue.SKIP,
        confidence=confidence,
        reason=reason,
        resume_match_signals=[],
        risk_flags=[],
    )
    store.record_job_decision(job_id, decision)


def latest_job_decision_reason(job_id: int) -> str | None:
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT reason
            FROM llm_decisions
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    return str(row["reason"]) if row else None


def sanitize_decision(
    decision: LlmJobDecision,
    job: JobCard,
    policy: AutonomousPolicy,
) -> LlmJobDecision:
    filtered_flags = [
        flag
        for flag in decision.risk_flags
        if not _matches_any_pattern(policy.ignored_model_risk_patterns, flag)
    ]
    if filtered_flags != decision.risk_flags:
        return LlmJobDecision(
            decision=decision.decision,
            confidence=decision.confidence,
            reason=decision.reason,
            resume_match_signals=decision.resume_match_signals,
            risk_flags=filtered_flags,
            raw_response=decision.raw_response,
        )
    return decision


def _match_detail_card(cards: list[JobCard], detail_url: str) -> JobCard | None:
    for card in cards:
        if card.detail_url and detail_url and card.detail_url == detail_url:
            return card
    return None


def _prefer_detail(list_value: str, detail_value: str | None, placeholder: str) -> str:
    """Keep a real list value, but let detail-page data replace a placeholder."""
    if list_value and list_value != placeholder:
        return list_value
    return detail_value or list_value


def merge_detail_job(list_job: JobCard, detail_html: str, detail_url: str) -> JobCard:
    text = html_to_text(detail_html)
    extracted = adapter.extract_job_cards(detail_html, source_url=detail_url)
    detail = _match_detail_card(extracted, detail_url)
    return JobCard(
        platform_job_id=list_job.platform_job_id,
        title=_prefer_detail(list_job.title, detail.title if detail else None, "Unknown role"),
        company=_prefer_detail(
            list_job.company, detail.company if detail else None, "Unknown company"
        ),
        source_url=list_job.source_url,
        detail_url=detail_url,
        salary=(detail.salary if detail and detail.salary else list_job.salary),
        location=list_job.location or (detail.location if detail else None),
        raw_text=trim_detail_text_for_decision(text),
    )


async def open_html(url: str, *, settle_seconds: float = 7.0) -> tuple[Any, str, str]:
    last_error: Exception | None = None
    for attempt in range(3):
        target = await ensure_page_target(
            url,
            url_contains="zhipin.com",
            settle_seconds=settle_seconds,
            retries=2,
            display=":1",
        )
        await asyncio.sleep(1.0 + attempt)
        try:
            html = await page_html(target)
            text = await page_text(target)
        except CdpError as exc:
            last_error = exc
            emit(
                "cdp_read_retry",
                url=url,
                attempt=attempt + 1,
                error=str(exc)[:500],
            )
            await asyncio.sleep(2.0)
            continue
        return target, html, text
    raise CdpError(f"Could not read page after retries: {url}; last error: {last_error}")


async def _visible_locator_items(locator: Any) -> list[Any]:
    items: list[Any] = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=1000):
                items.append(item)
        except Exception:
            continue
    return items


async def playwright_click_immediate_contact(detail_url: str) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(default_cdp_url())
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (
                candidate
                for candidate in pages
                if target_url_matches(detail_url, candidate.url, "zhipin.com")
            ),
            None,
        )
        if page is None:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
        await page.bring_to_front()
        await page.wait_for_timeout(500)

        before_url = page.url
        body_text = await page.locator("body").inner_text(timeout=5000)
        risks = visible_page_risk_payload(body_text)
        if risks:
            return {
                "ok": False,
                "reason": "page_risk_before_playwright_click",
                "before_url": before_url,
                "post_click_risks": risks,
            }

        # Stay inside the selected job's box so a recommendation/sidebar card's
        # control is never matched (mirrors the main CDP path).
        detail_box = page.locator(".job-detail-box")
        existing_locators = []
        for selector in (".btn-startchat", ".btn-startchat-wrap", "a,button,[role=button]"):
            existing_locators.extend(
                await _visible_locator_items(
                    detail_box.locator(selector).filter(has_text=re.compile("继续沟通|沟通中"))
                )
            )
            if len(existing_locators) == 1:
                label = (await existing_locators[0].inner_text(timeout=1000)).strip()
                return {
                    "ok": True,
                    "reason": "already_in_conversation",
                    "clicked_label": label or "继续沟通",
                    "before_url": before_url,
                    "post_click_url": page.url,
                    "post_click_verified": True,
                    "post_click_risks": [],
                }

        contact_locators = []
        for selector in (".btn-startchat", ".btn-startchat-wrap", "a,button,[role=button]"):
            contact_locators = await _visible_locator_items(
                detail_box.locator(selector).filter(has_text="立即沟通")
            )
            if contact_locators:
                break
        if len(contact_locators) != 1:
            return {
                "ok": False,
                "reason": "playwright_contact_button_not_unique",
                "count": len(contact_locators),
                "before_url": before_url,
            }

        button = contact_locators[0]
        button_text = (await button.inner_text(timeout=1000)).strip()
        button_box = await button.bounding_box()
        before_text = await page.locator("body").inner_text(timeout=5000)
        await button.click(timeout=10000)

        post_text = ""
        post_risks: list[dict[str, str]] = []
        verified = False
        for _ in range(12):
            await page.wait_for_timeout(1000)
            post_text = await page.locator("body").inner_text(timeout=5000)
            post_risks = visible_page_risk_payload(post_text)
            # Only a marker that newly appears after the click is evidence; markers
            # already present beforehand (nav, an existing chat panel) are not.
            verified = not post_risks and any(
                marker in post_text and marker not in before_text for marker in SUCCESS_MARKERS
            )
            if verified or post_risks:
                break
        return {
            "ok": True,
            "reason": "playwright_click_attempted",
            "clicked_label": button_text or "立即沟通",
            "before_url": before_url,
            "post_click_url": page.url,
            "post_click_verified": verified,
            "post_click_risks": post_risks,
            "button_box": button_box,
        }
    finally:
        await playwright.stop()


async def _precheck(awaitable: Any) -> Any:
    """Await a pre-click CDP operation, tagging infra failures as pre-click aborts.

    A stale or disconnected CDP target can fail these reads before any mouse event
    is dispatched. Tagging the error with a PRE_CLICK_ABORT_PREFIXES prefix lets
    process_job release the reservation instead of confirming a contact that never
    happened.
    """
    try:
        return await awaitable
    except Exception as exc:
        message = str(exc)
        if message.startswith(PRE_CLICK_ABORT_PREFIXES):
            raise
        raise RuntimeError(f"contact_button_not_safe:precheck:{message}") from exc


async def click_immediate_contact(
    target: Any,
    platform_job_id: str,
    detail_url: str,
) -> dict[str, Any]:
    current_url = str(
        await _precheck(cdp_evaluate(target.web_socket_debugger_url, "window.location.href"))
        or ""
    )
    if detail_url and not target_url_matches(detail_url, current_url, "zhipin.com"):
        await _precheck(cdp_navigate(target.web_socket_debugger_url, detail_url))
        await asyncio.sleep(3.0)
        post_nav_url = str(
            await _precheck(cdp_evaluate(target.web_socket_debugger_url, "window.location.href"))
            or ""
        )
        if not target_url_matches(detail_url, post_nav_url, "zhipin.com"):
            raise RuntimeError(f"contact_button_not_safe:navigation_drift:{post_nav_url}")

    before_text = await _precheck(page_text(target))
    risks = visible_page_risk_payload(before_text)
    if risks:
        raise RuntimeError(f"page_risk_before_click:{risks}")
    await _precheck(cdp_bring_to_front(target.web_socket_debugger_url))
    await asyncio.sleep(0.25)
    js = r"""
    (() => {
      window.__resumePilotClickEvents = [];
      if (!window.__resumePilotClickCaptureInstalled) {
        for (const type of ['mousemove', 'mousedown', 'mouseup', 'click']) {
          document.addEventListener(type, (event) => {
            const target = event.target;
            window.__resumePilotClickEvents.push({
              type,
              trusted: event.isTrusted,
              target: target ? target.tagName + '.' + String(target.className || '') : null,
              text: target && target.innerText
                ? target.innerText.replace(/\s+/g, ' ').trim().slice(0, 80)
                : '',
              x: event.clientX,
              y: event.clientY
            });
          }, true);
        }
        window.__resumePilotClickCaptureInstalled = true;
      }
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style && style.visibility !== 'hidden' && style.display !== 'none'
          && rect.width > 0 && rect.height > 0;
      };
      const hitTested = (el) => {
        const rect = el.getBoundingClientRect();
        const top = document.elementFromPoint(
          rect.left + rect.width / 2,
          rect.top + rect.height / 2
        );
        return top && (el === top || el.contains(top));
      };
      const scope = document.querySelector('.job-detail-box');
      if (!scope) {
        return {ok: false, reason: 'visible_contact_button_not_unique', count: 0};
      }
      const recExclude =
        '[class*="recommend"], .look-job, .similar-job, .job-card-wrapper, .job-list';
      const findMatches = (selector, labels) => Array.from(
        scope.querySelectorAll(selector)
      ).filter((el) => {
        if (el.closest(recExclude)) {
          return false;
        }
        const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (!labels.some((label) => text.includes(label)) || !visible(el)) {
          return false;
        }
        return hitTested(el);
      });
      const selectorGroups = ['.btn-startchat', '.btn-startchat-wrap', 'a,button,[role="button"]'];
      const existingConversationLabels = ['继续沟通', '沟通中'];
      let matches = [];
      for (const selector of selectorGroups) {
        matches = findMatches(selector, existingConversationLabels);
        if (matches.length === 1) {
          const text = (matches[0].innerText || '').replace(/\s+/g, ' ').trim();
          return {
            ok: true,
            reason: 'already_in_conversation',
            count: 1,
            clicked: false,
            label: text
          };
        }
      }
      for (const selector of selectorGroups) {
        matches = findMatches(selector, ['立即沟通']);
        if (matches.length === 1) {
          break;
        }
      }
      if (matches.length !== 1) {
        return {ok: false, reason: 'visible_contact_button_not_unique', count: matches.length};
      }
      const rect = matches[0].getBoundingClientRect();
      const link = matches[0].matches('a')
        ? matches[0]
        : matches[0].querySelector('a[redirect-url]');
      return {
        ok: true,
        reason: 'clickable_center_found',
        count: 1,
        redirect_url: link ? link.getAttribute('redirect-url') : null,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2
      };
    })()
    """
    result = await _precheck(cdp_evaluate(target.web_socket_debugger_url, js))
    if not isinstance(result, dict) or not result.get("ok"):
        if (
            isinstance(result, dict)
            and result.get("reason") == "visible_contact_button_not_unique"
            and result.get("count") == 0
        ):
            raise RuntimeError(f"contact_button_unavailable:{result}")
        raise RuntimeError(f"contact_button_not_safe:{result}")
    if result.get("reason") == "already_in_conversation":
        return {
            "clicked_label": str(result.get("label") or "继续沟通"),
            "job_id": platform_job_id,
            "before_url": detail_url,
            "already_in_conversation": True,
            "click_events": [],
            "fallback_navigation_used": False,
            "post_click_verified": True,
            "post_click_risks": [],
            "needs_manual_verification": False,
        }
    try:
        await cdp_dispatch_mouse_click(
            target.web_socket_debugger_url,
            float(result["x"]),
            float(result["y"]),
            bring_to_front=False,
        )
    except CdpClickError as exc:
        if exc.dispatched:
            # A mouse button event was already sent, so the click may have landed.
            # Let it reach the post-click (confirm) path rather than releasing and
            # risking a duplicate message to the same recruiter on a later run.
            raise
        # No mouse event was sent (failure before the press), so no platform click
        # landed — a pre-click abort that releases the reservation.
        raise RuntimeError(f"contact_button_not_safe:dispatch_failed:{exc}") from exc
    fallback_navigation_used = False
    post_text = ""
    post_risks: list[dict[str, str]] = []
    markers_seen = False
    click_events: list[dict[str, Any]] = []
    for _ in range(20):
        await asyncio.sleep(1.0)
        post_text = await page_text(target)
        post_risks = visible_page_risk_payload(post_text)
        # Require the marker to newly appear after the click; markers already
        # visible before the click (nav, another conversation) are not evidence.
        markers_seen = any(
            marker in post_text and marker not in before_text for marker in SUCCESS_MARKERS
        )
        if markers_seen or post_risks:
            break
    raw_click_events = await cdp_evaluate(
        target.web_socket_debugger_url,
        "window.__resumePilotClickEvents || []",
    )
    if isinstance(raw_click_events, list):
        click_events = [item for item in raw_click_events if isinstance(item, dict)]
    trusted_click_events = [
        event
        for event in click_events
        if event.get("type") == "click" and event.get("trusted")
    ]
    trusted_contact_click_reached_page = any(
        "btn-startchat" in str(event.get("target") or "")
        or "沟通" in str(event.get("text") or "")
        for event in trusted_click_events
    )
    trusted_wrong_click_reached_page = any(
        not (
            "btn-startchat" in str(event.get("target") or "")
            or "沟通" in str(event.get("text") or "")
        )
        for event in trusted_click_events
    )
    trusted_click_reached_page = any(
        event.get("type") == "click" and event.get("trusted") for event in click_events
    )
    # Require a trusted click on the contact control itself; generic conversation
    # markers can already be present in site navigation before any effective click.
    verified = not post_risks and markers_seen and trusted_contact_click_reached_page
    post_click_url = str(
        await cdp_evaluate(target.web_socket_debugger_url, "window.location.href") or ""
    )
    playwright_details: dict[str, Any] = {}
    if (
        not verified
        and not post_risks
        and not trusted_click_reached_page
        and not trusted_wrong_click_reached_page
    ):
        try:
            playwright_details = await playwright_click_immediate_contact(detail_url)
        except Exception as exc:
            playwright_details = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if playwright_details.get("post_click_verified"):
            return {
                "clicked_label": str(playwright_details.get("clicked_label") or "立即沟通"),
                "job_id": platform_job_id,
                "before_url": detail_url,
                # No already_in_conversation here: this branch runs after the CDP
                # click was dispatched, so a "继续沟通" the fallback sees may be the
                # result of that click. Confirm the contact rather than releasing it
                # and risking a duplicate message on a later run.
                "click_events": click_events[-8:],
                "fallback_navigation_used": False,
                "post_click_verified": True,
                "post_click_risks": playwright_details.get("post_click_risks") or [],
                "trusted_contact_click_reached_page": trusted_contact_click_reached_page,
                "trusted_wrong_click_reached_page": trusted_wrong_click_reached_page,
                "needs_manual_verification": False,
                "cdp_click": {
                    "x": result.get("x"),
                    "y": result.get("y"),
                    "redirect_url": result.get("redirect_url"),
                    "post_click_url": post_click_url,
                },
                "playwright_click": playwright_details,
            }
    return {
        "clicked_label": "立即沟通",
        "job_id": platform_job_id,
        "before_url": detail_url,
        "click_events": click_events[-8:],
        "fallback_navigation_used": fallback_navigation_used,
        "post_click_verified": verified,
        "post_click_risks": post_risks,
        "trusted_contact_click_reached_page": trusted_contact_click_reached_page,
        "trusted_wrong_click_reached_page": trusted_wrong_click_reached_page,
        "needs_manual_verification": not verified,
        "cdp_click": {
            "x": result.get("x"),
            "y": result.get("y"),
            "redirect_url": result.get("redirect_url"),
            "post_click_url": post_click_url,
        },
        "playwright_click": playwright_details,
    }


async def process_job(
    list_job: JobCard,
    *,
    policy: AutonomousPolicy,
    profile_summary: str,
) -> bool:
    list_salary_reason = salary_skip_reason(list_job, policy)
    if list_salary_reason and list_salary_reason != "salary_missing_or_unparseable":
        job_id, inserted = store.upsert_job(list_job)
        record_skip(job_id, list_salary_reason)
        emit(
            "skip",
            title=list_job.title,
            salary=list_job.salary,
            reason=list_salary_reason,
            inserted=inserted,
        )
        return True
    if not is_allowed_boss_detail_url(list_job.detail_url):
        emit("skip", title=list_job.title, salary=list_job.salary, reason="unsupported_detail_url")
        return True
    known = store.get_job_by_platform_id(list_job.platform_job_id)
    if known and store.has_action(int(known["id"]), ApplicationAction.IMMEDIATE_CONTACT):
        emit("skip", title=list_job.title, salary=list_job.salary, reason="already_contacted")
        return True
    detail_target, detail_html, detail_text = await open_html(
        list_job.detail_url,
        settle_seconds=6.0,
    )
    risks = visible_page_risk_payload(detail_text)
    if risks:
        pause("page_risk_on_detail", title=list_job.title, url=list_job.detail_url, risks=risks)
        return False
    job = merge_detail_job(list_job, detail_html, list_job.detail_url)
    job_id, inserted = store.upsert_job(job)
    emit("detail", title=job.title, salary=job.salary, url=job.detail_url, inserted=inserted)

    if store.has_action(job_id, ApplicationAction.IMMEDIATE_CONTACT):
        emit("skip", title=job.title, salary=job.salary, reason="already_contacted")
        return True

    salary_reason = salary_skip_reason(job, policy)
    if salary_reason:
        record_skip(job_id, salary_reason)
        emit("skip", title=job.title, salary=job.salary, reason=salary_reason)
        return True

    prompt = build_job_decision_prompt(job, profile_summary)
    try:
        decision = sanitize_decision(
            parse_job_decision(client.run_json(prompt)),
            job,
            policy,
        )
    except (InvalidLlmResponseError, LlmError, TimeoutError) as exc:
        if isinstance(exc, LlmError) and "timed out" in str(exc):
            record_skip(job_id, "llm_decision_timeout", confidence=0.0)
            emit(
                "skip",
                title=job.title,
                salary=job.salary,
                reason="llm_decision_timeout",
                error=str(exc),
            )
            return True
        pause("llm_decision_failed", title=job.title, url=job.detail_url, error=str(exc))
        return False
    store.record_job_decision(job_id, decision)
    emit(
        "decision",
        title=job.title,
        decision=decision.decision.value,
        confidence=decision.confidence,
        risk_flags=decision.risk_flags,
        reason=decision.reason[:500],
    )

    if decision.decision != LlmJobDecisionValue.APPLY:
        return True
    if decision.confidence < 0.75 or decision.risk_flags:
        record_skip(
            job_id,
            (
                "apply_decision_not_safe:"
                f"confidence={decision.confidence};risk_flags={decision.risk_flags}"
            ),
            confidence=decision.confidence,
        )
        emit(
            "skip",
            title=job.title,
            salary=job.salary,
            reason="apply_decision_not_safe",
            confidence=decision.confidence,
            risk_flags=decision.risk_flags,
        )
        return True
    if not store.can_record_contact(daily_cap=policy.daily_cap):
        emit(
            "cap_reached",
            today=store.action_count(ApplicationAction.IMMEDIATE_CONTACT),
            daily_cap=policy.daily_cap,
        )
        return False
    if store.has_active_action_attempt(job_id, ApplicationAction.IMMEDIATE_CONTACT):
        pause("active_contact_attempt_exists", title=job.title, url=job.detail_url)
        return False

    try:
        store.reserve_contact(
            job_id,
            daily_cap=policy.daily_cap,
            details={"job_id": job.platform_job_id, "url": job.detail_url},
        )
    except DuplicateActionError:
        emit("skip", title=job.title, salary=job.salary, reason="already_contacted")
        return True
    except BudgetExceededError:
        emit(
            "cap_reached",
            today=store.action_count(ApplicationAction.IMMEDIATE_CONTACT),
            daily_cap=policy.daily_cap,
        )
        return False

    attempt_id = store.start_action_attempt(
        job_id,
        ApplicationAction.IMMEDIATE_CONTACT,
        details={"job_id": job.platform_job_id, "url": job.detail_url},
    )
    try:
        details = await click_immediate_contact(
            detail_target,
            job.platform_job_id,
            job.detail_url or "",
        )
    except Exception as exc:
        message = str(exc)
        store.finish_action_attempt(
            attempt_id,
            status="failed",
            details={"error": message, "job_id": job.platform_job_id},
        )
        if message.startswith(PRE_CLICK_ABORT_PREFIXES):
            store.release_contact(job_id)
            if message.startswith("contact_button_unavailable:"):
                record_skip(job_id, "contact_button_unavailable", confidence=0.0)
                emit(
                    "skip",
                    title=job.title,
                    salary=job.salary,
                    reason="contact_button_unavailable",
                    error=message[:500],
                )
                return True
            pause("contact_click_failed", title=job.title, url=job.detail_url, error=message)
            return False
        store.confirm_contact(
            job_id,
            details={"job_id": job.platform_job_id, "error": message, "post_click_failure": True},
        )
        pause(
            "contact_failed_after_possible_click",
            title=job.title,
            url=job.detail_url,
            error=message,
        )
        return False

    details = {**details, "attempt_id": attempt_id}
    store.finish_action_attempt(attempt_id, status="clicked", details=details)
    if details.get("already_in_conversation"):
        store.release_contact(job_id)
        store.finish_action_attempt(
            attempt_id, status="already_in_conversation", details=details
        )
        emit("already_in_conversation", title=job.title, url=job.detail_url)
        return True
    store.confirm_contact(job_id, details=details)
    store.finish_action_attempt(attempt_id, status="recorded", details=details)
    if details.get("needs_manual_verification"):
        pause(
            "contact_click_needs_manual_verification",
            title=job.title,
            url=job.detail_url,
            details=details,
        )
        return False
    today = store.action_count(ApplicationAction.IMMEDIATE_CONTACT)
    emit("contacted", today=today, title=job.title, salary=job.salary, post_url=job.detail_url)
    return today < policy.daily_cap


async def main() -> int:
    policy = AutonomousPolicy.load(paths)
    profile_summary, keywords = load_profile(policy)
    current = store.action_count(ApplicationAction.IMMEDIATE_CONTACT)
    emit(
        "start",
        daily_cap=policy.daily_cap,
        current_count=current,
        minimum_monthly_salary_k=policy.minimum_monthly_salary_k,
        keyword_count=len(keywords),
    )
    active = store.active_pauses()
    if active:
        # Refuse to open pages or contact more jobs while a prior run's pause is
        # unresolved; manual takeover must clear it first.
        emit(
            "blocked_by_active_pauses",
            count=len(active),
            reasons=[pause_row["reason"] for pause_row in active],
        )
        return 2
    if current >= policy.daily_cap:
        emit("cap_reached", today=current, daily_cap=policy.daily_cap)
        return 0
    manager = BrowserManager(paths)
    if manager.cdp_host not in LOOPBACK_CDP_HOSTS:
        pause("cdp_host_not_loopback", cdp_url=manager.cdp_url, cdp_host=manager.cdp_host)
        return 2
    browser_status = manager.status()
    if not browser_status.running:
        pause(
            "managed_browser_not_running",
            detail=browser_status.detail,
            cdp_url=browser_status.cdp_url,
        )
        return 2
    seen: set[str] = set()
    for keyword in keywords:
        url = search_url(keyword, policy)
        emit("open_search", keyword=keyword, url=url)
        try:
            _, html, text = await open_html(url, settle_seconds=8.0)
        except CdpError as exc:
            pause("open_search_failed", keyword=keyword, url=url, error=str(exc))
            return 2
        risks = visible_page_risk_payload(text)
        if risks:
            pause("page_risk_on_search", keyword=keyword, url=url, risks=risks)
            return 2
        jobs = adapter.extract_job_cards(html, source_url=url, include_detail_pane=False)
        emit("jobs_found", keyword=keyword, count=len(jobs))
        for list_job in jobs:
            if list_job.platform_job_id in seen:
                continue
            seen.add(list_job.platform_job_id)
            keep_going = await process_job(
                list_job,
                policy=policy,
                profile_summary=profile_summary,
            )
            if not keep_going:
                # A paused stop needs manual takeover; only cap/complete may exit 0
                # so cron/systemd treats an unfinished run as a failure.
                return 2 if store.active_pauses() else 0
            if store.action_count(ApplicationAction.IMMEDIATE_CONTACT) >= policy.daily_cap:
                emit(
                    "cap_reached",
                    today=store.action_count(ApplicationAction.IMMEDIATE_CONTACT),
                    daily_cap=policy.daily_cap,
                )
                return 0
    emit(
        "complete",
        today=store.action_count(ApplicationAction.IMMEDIATE_CONTACT),
        daily_cap=policy.daily_cap,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        emit("interrupted")
        raise
    except Exception as exc:
        pause("unhandled_exception", error=str(exc), type=type(exc).__name__)
        raise
