from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_remote_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_PILOT_STATE_DB", str(tmp_path / "state.sqlite"))
    module_path = Path(__file__).resolve().parents[1] / "ops/remote/autonomous_apply.py"
    spec = importlib.util.spec_from_file_location("remote_autonomous_apply_for_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_click_immediate_contact_accepts_existing_conversation(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    clicks = []

    async def fake_page_text(_target):
        return "Senior platform engineer\n继续沟通"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        return {
            "ok": True,
            "reason": "already_in_conversation",
            "count": 1,
            "clicked": False,
            "label": "继续沟通",
        }

    async def fake_click(*_args):
        clicks.append(True)

    async def fake_bring_to_front(*_args):
        return None

    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)

    details = asyncio.run(
        module.click_immediate_contact(
            target,
            platform_job_id="boss:example",
            detail_url=detail_url,
        )
    )

    assert clicks == []
    assert details["already_in_conversation"] is True
    assert details["post_click_verified"] is True
    assert details["needs_manual_verification"] is False


def test_click_immediate_contact_rejects_wrong_trusted_click(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    click_kwargs = []
    sleep_calls = []

    async def fake_sleep(_seconds):
        sleep_calls.append(_seconds)

    async def fake_page_text(_target):
        return "Senior platform engineer\n立即沟通"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return [
                {
                    "type": "click",
                    "trusted": True,
                    "target": "A.btn btn-interest",
                    "text": "感兴趣",
                    "x": 262,
                    "y": 223,
                }
            ]
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=wrong",
            "x": 262,
            "y": 223,
        }

    async def fake_bring_to_front(*_args):
        return None

    async def fake_click(*_args, **kwargs):
        click_kwargs.append(kwargs)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)

    details = asyncio.run(
        module.click_immediate_contact(
            target,
            platform_job_id="boss:example",
            detail_url=detail_url,
        )
    )

    assert click_kwargs == [{"bring_to_front": False}]
    assert details["fallback_navigation_used"] is False
    assert details["post_click_verified"] is False
    assert details["trusted_wrong_click_reached_page"] is True
    assert details["needs_manual_verification"] is True


def test_click_immediate_contact_uses_playwright_when_cdp_click_has_no_event(
    monkeypatch,
    tmp_path,
):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    playwright_calls = []
    navigate_calls = []

    async def fake_sleep(_seconds):
        return None

    async def fake_page_text(_target):
        return "Senior platform engineer\n立即沟通"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return []
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=ok",
            "x": 262,
            "y": 223,
        }

    async def fake_bring_to_front(*_args):
        return None

    async def fake_click(*_args, **_kwargs):
        return None

    async def fake_playwright_click(url):
        playwright_calls.append(url)
        return {
            "ok": True,
            "reason": "playwright_click_attempted",
            "clicked_label": "立即沟通",
            "post_click_verified": True,
            "post_click_risks": [],
            "post_click_url": "https://www.zhipin.com/web/geek/chat",
        }

    async def fake_navigate(*args):
        navigate_calls.append(args)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)
    monkeypatch.setattr(module, "playwright_click_immediate_contact", fake_playwright_click)
    monkeypatch.setattr(module, "cdp_navigate", fake_navigate)

    details = asyncio.run(
        module.click_immediate_contact(
            target,
            platform_job_id="boss:example",
            detail_url=detail_url,
        )
    )

    assert playwright_calls == [detail_url]
    assert navigate_calls == []
    assert details["post_click_verified"] is True
    assert details["needs_manual_verification"] is False
    assert details["playwright_click"]["reason"] == "playwright_click_attempted"


def test_click_immediate_contact_reports_unavailable_button(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")

    async def fake_sleep(_seconds):
        return None

    async def fake_page_text(_target):
        return "Senior platform engineer"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        return {
            "ok": False,
            "reason": "visible_contact_button_not_unique",
            "count": 0,
        }

    async def fake_bring_to_front(*_args):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)

    try:
        asyncio.run(
            module.click_immediate_contact(
                target,
                platform_job_id="boss:example",
                detail_url=detail_url,
            )
        )
    except RuntimeError as exc:
        assert str(exc).startswith("contact_button_unavailable:")
    else:
        raise AssertionError("expected RuntimeError")


def make_policy(module, tmp_path):
    return module.AutonomousPolicy(
        daily_cap=150,
        city="sample-city-code",
        salary_param="sample-salary-code",
        minimum_monthly_salary_k=40,
        search_keywords=["Kubernetes"],
        role_include_terms=["K8s", "Kubernetes", "platform"],
        title_reject_terms=["DisallowedRole"],
        ignored_model_risk_patterns=[re.compile("EmployerSideOnly", re.IGNORECASE)],
        profile_analysis_path=tmp_path / "profile-analysis.json",
        include_profile_keywords=False,
        llm_policy={},
    )


def make_job(module):
    return module.JobCard(
        platform_job_id="boss:sample-k8s",
        title="Kubernetes Platform Engineer",
        company="Example",
        source_url="https://www.zhipin.com/web/geek/jobs?query=Kubernetes",
        detail_url="https://www.zhipin.com/job_detail/example.html",
        salary="40-55K",
        location="Sample City",
        raw_text="K8s platform engineering",
    )


def test_policy_load_allows_missing_role_guidance(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "daily_cap": 25,
                "search_keywords": ["target-role-keyword"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(module.POLICY_ENV, str(policy_path))

    app_paths = module.AppPaths(
        state_db=tmp_path / "state.sqlite",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        chrome_profile=tmp_path / "chrome-profile",
        profile_cache=tmp_path / "profile.json",
        browser_pid=tmp_path / "browser.pid",
        browser_log=tmp_path / "browser.log",
    )

    policy = module.AutonomousPolicy.load(app_paths)

    assert policy.role_include_terms == []
    assert policy.title_reject_terms == []
    assert policy.minimum_monthly_salary_k is None


def test_role_policy_context_does_not_skip_before_llm(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    calls = []

    class FakeClient:
        def run_json(self, prompt):
            calls.append(prompt)
            return json.dumps(
                {
                    "decision": "skip",
                    "confidence": 0.9,
                    "reason": "Model evaluated the private policy context.",
                    "resume_match_signals": [],
                    "risk_flags": [],
                }
            )

    async def fake_open_html(_url, *, settle_seconds):
        return (
            SimpleNamespace(web_socket_debugger_url="ws://target"),
            "<html><body>Role detail</body></html>",
            "Role detail",
        )

    policy = make_policy(module, tmp_path)
    list_job = module.JobCard(
        platform_job_id="boss:role-policy",
        title="DisallowedRole Database Administrator",
        company="Example",
        source_url="https://www.zhipin.com/web/geek/jobs?query=target-role-keyword",
        detail_url="https://www.zhipin.com/job_detail/role-policy.html",
        salary="40-55K",
        location="Sample City",
        raw_text="Role text that previously matched source-code prefilters.",
    )

    monkeypatch.setattr(module, "client", FakeClient())
    monkeypatch.setattr(module, "open_html", fake_open_html)

    keep_going = asyncio.run(
        module.process_job(list_job, policy=policy, profile_summary="Private policy.")
    )

    assert keep_going is True
    assert len(calls) == 1
    job = module.store.get_job_by_platform_id("boss:role-policy")
    assert job is not None
    assert (
        module.latest_job_decision_reason(job["id"])
        == "Model evaluated the private policy context."
    )


def test_model_apply_is_not_downgraded_by_code_text_heuristics(
    monkeypatch,
    tmp_path,
):
    module = load_remote_module(monkeypatch, tmp_path)
    decision = module.LlmJobDecision(
        decision=module.LlmJobDecisionValue.APPLY,
        confidence=0.9,
        reason="The model judged the role against the private candidate policy.",
        resume_match_signals=["K8s"],
        risk_flags=["This text is model evidence, not hard-coded application policy."],
    )

    sanitized = module.sanitize_decision(decision, make_job(module), make_policy(module, tmp_path))

    assert sanitized.decision == module.LlmJobDecisionValue.APPLY
    assert sanitized.risk_flags == decision.risk_flags
    assert sanitized.reason == decision.reason


def test_model_skip_is_not_promoted_by_ignored_risk_patterns(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    decision = module.LlmJobDecision(
        decision=module.LlmJobDecisionValue.SKIP,
        confidence=0.9,
        reason="The model decided this is not a candidate-side match.",
        resume_match_signals=["K8s"],
        risk_flags=["EmployerSideOnly requirement is ignored but the decision stays skip"],
    )

    sanitized = module.sanitize_decision(decision, make_job(module), make_policy(module, tmp_path))

    assert sanitized.decision == module.LlmJobDecisionValue.SKIP
    assert sanitized.risk_flags == []
    assert "ignored-model-risk" not in sanitized.reason


def test_is_allowed_boss_detail_url_rejects_foreign_and_insecure_hosts(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    assert module.is_allowed_boss_detail_url("https://www.zhipin.com/job_detail/abc.html")
    assert not module.is_allowed_boss_detail_url("https://evil.example/job_detail/abc?zhipin.com")
    assert not module.is_allowed_boss_detail_url("http://www.zhipin.com/job_detail/abc.html")
    assert not module.is_allowed_boss_detail_url("https://www.zhipin.com/web/geek/jobs")
    assert not module.is_allowed_boss_detail_url(None)


def test_apply_with_risk_flags_is_not_contacted(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    clicked = []

    class FakeClient:
        def run_json(self, _prompt):
            return json.dumps(
                {
                    "decision": "apply",
                    "confidence": 0.9,
                    "reason": "Model surfaced an unresolved risk",
                    "resume_match_signals": ["K8s"],
                    "risk_flags": ["unverified compensation"],
                }
            )

    async def fake_open_html(_url, *, settle_seconds):
        return (
            SimpleNamespace(web_socket_debugger_url="ws://target"),
            "<html><body>立即沟通 Role detail</body></html>",
            "Role detail",
        )

    async def fake_click(*_args, **_kwargs):
        clicked.append(True)
        return {"needs_manual_verification": False}

    monkeypatch.setattr(module, "client", FakeClient())
    monkeypatch.setattr(module, "open_html", fake_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(make_job(module), policy=make_policy(module, tmp_path),
                           profile_summary="Private policy.")
    )

    assert keep_going is True
    assert clicked == []
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert not module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT)
    assert module.latest_job_decision_reason(job["id"]).startswith("apply_decision_not_safe")


def test_unverified_click_records_contact_to_preserve_dedupe(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    class FakeClient:
        def run_json(self, _prompt):
            return json.dumps(
                {
                    "decision": "apply",
                    "confidence": 0.95,
                    "reason": "Strong candidate-side match",
                    "resume_match_signals": ["K8s"],
                    "risk_flags": [],
                }
            )

    async def fake_open_html(_url, *, settle_seconds):
        return (
            SimpleNamespace(web_socket_debugger_url="ws://target"),
            "<html><body>立即沟通 Role detail</body></html>",
            "Role detail",
        )

    async def fake_click(*_args, **_kwargs):
        return {
            "clicked_label": "立即沟通",
            "post_click_verified": False,
            "needs_manual_verification": True,
        }

    monkeypatch.setattr(module, "client", FakeClient())
    monkeypatch.setattr(module, "open_html", fake_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(make_job(module), policy=make_policy(module, tmp_path),
                           profile_summary="Private policy.")
    )

    assert keep_going is False
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT)


def test_unverified_cdp_click_does_not_navigate_redirect_url(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    navigate_calls = []

    async def fake_sleep(_seconds):
        return None

    async def fake_page_text(_target):
        return "Senior platform engineer\n立即沟通"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return []
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=ok",
            "x": 262,
            "y": 223,
        }

    async def fake_bring_to_front(*_args):
        return None

    async def fake_click(*_args, **_kwargs):
        return None

    async def fake_playwright_click(_url):
        return {"ok": False, "post_click_verified": False, "post_click_risks": []}

    async def fake_navigate(*args):
        navigate_calls.append(args)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)
    monkeypatch.setattr(module, "playwright_click_immediate_contact", fake_playwright_click)
    monkeypatch.setattr(module, "cdp_navigate", fake_navigate)

    details = asyncio.run(
        module.click_immediate_contact(
            target,
            platform_job_id="boss:example",
            detail_url=detail_url,
        )
    )

    assert navigate_calls == []
    assert details["fallback_navigation_used"] is False
    assert details["needs_manual_verification"] is True


def test_merge_detail_job_ignores_unrelated_recommendation_salary(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    list_job = module.JobCard(
        platform_job_id="boss:main",
        title="Platform Engineer",
        company="Example",
        source_url="https://www.zhipin.com/web/geek/jobs",
        detail_url="https://www.zhipin.com/job_detail/main.html",
        salary="40-55K",
        location="Sample City",
        raw_text="main role",
    )
    detail_html = """
    <li class="job-card-wrapper" data-job-id="reco">
      <a class="job-name" href="/job_detail/reco.html">Recommended Role</a>
      <span class="salary">8-10K</span>
      <span class="company-name">Other Co</span>
    </li>
    """

    merged = module.merge_detail_job(list_job, detail_html, list_job.detail_url)

    assert merged.salary == "40-55K"


def _apply_client(module):
    class FakeClient:
        def run_json(self, _prompt):
            return json.dumps(
                {
                    "decision": "apply",
                    "confidence": 0.95,
                    "reason": "Strong candidate-side match",
                    "resume_match_signals": ["K8s"],
                    "risk_flags": [],
                }
            )

    return FakeClient()


async def _detail_open_html(_url, *, settle_seconds):
    return (
        SimpleNamespace(web_socket_debugger_url="ws://target"),
        "<html><body>立即沟通 Role detail</body></html>",
        "Role detail",
    )


def test_already_in_conversation_does_not_consume_daily_cap(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_click(*_args, **_kwargs):
        return {
            "clicked_label": "继续沟通",
            "already_in_conversation": True,
            "post_click_verified": True,
            "needs_manual_verification": False,
        }

    monkeypatch.setattr(module, "client", _apply_client(module))
    monkeypatch.setattr(module, "open_html", _detail_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(make_job(module), policy=make_policy(module, tmp_path),
                           profile_summary="Private policy.")
    )

    assert keep_going is True
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT) is False
    assert module.store.action_count(module.ApplicationAction.IMMEDIATE_CONTACT) == 0


def test_post_click_failure_keeps_reservation(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_click(*_args, **_kwargs):
        raise RuntimeError("websocket read failed after click")

    monkeypatch.setattr(module, "client", _apply_client(module))
    monkeypatch.setattr(module, "open_html", _detail_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(make_job(module), policy=make_policy(module, tmp_path),
                           profile_summary="Private policy.")
    )

    assert keep_going is False
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT) is True


def test_pre_click_failure_releases_reservation(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_click(*_args, **_kwargs):
        raise RuntimeError("contact_button_not_safe:{'reason': 'risk'}")

    monkeypatch.setattr(module, "client", _apply_client(module))
    monkeypatch.setattr(module, "open_html", _detail_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(make_job(module), policy=make_policy(module, tmp_path),
                           profile_summary="Private policy.")
    )

    assert keep_going is False
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT) is False


def test_bootstrap_installs_iproute2_for_ss():
    bootstrap = (
        Path(__file__).resolve().parents[1] / "ops/remote/bootstrap_debian.sh"
    ).read_text(encoding="utf-8")
    assert "iproute2" in bootstrap


def test_navigation_drift_aborts_before_click(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    navigated = []
    urls = iter(
        [
            "https://www.zhipin.com/web/geek/jobs",
            "https://www.zhipin.com/job_detail/other.html",
        ]
    )

    async def fake_sleep(_seconds):
        return None

    async def fake_cdp_evaluate(_ws, expression):
        if expression == "window.location.href":
            return next(urls)
        return {}

    async def fake_navigate(*args):
        navigated.append(args)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_navigate", fake_navigate)

    try:
        asyncio.run(
            module.click_immediate_contact(
                target, platform_job_id="boss:example", detail_url=detail_url
            )
        )
    except RuntimeError as exc:
        assert str(exc).startswith("contact_button_not_safe:navigation_drift")
    else:
        raise AssertionError("expected RuntimeError on navigation drift")

    assert len(navigated) == 1


def test_already_contacted_job_is_skipped_before_opening_detail(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    job = make_job(module)
    job_id, _ = module.store.upsert_job(job)
    module.store.record_contact(job_id, daily_cap=150)

    opened = []

    async def fail_open_html(url, *, settle_seconds):
        opened.append(url)
        raise AssertionError("must not open the detail page for an already-contacted job")

    monkeypatch.setattr(module, "open_html", fail_open_html)

    keep_going = asyncio.run(
        module.process_job(job, policy=make_policy(module, tmp_path), profile_summary="Policy.")
    )

    assert keep_going is True
    assert opened == []


def test_autonomous_client_honors_claude_model_override(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_PILOT_CLAUDE_MODEL", "custom-model-alias")
    module = load_remote_module(monkeypatch, tmp_path)

    assert module.client.model == "custom-model-alias"


def test_autonomous_post_click_failure_confirms_and_pauses(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_click(*_args, **_kwargs):
        raise RuntimeError("post-click verification read failed")

    monkeypatch.setattr(module, "client", _apply_client(module))
    monkeypatch.setattr(module, "open_html", _detail_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(
            make_job(module), policy=make_policy(module, tmp_path), profile_summary="Policy."
        )
    )

    assert keep_going is False
    job = module.store.get_job_by_platform_id("boss:sample-k8s")
    assert job["status"] == "awaiting_reply"
    assert module.store.has_action(job["id"], module.ApplicationAction.IMMEDIATE_CONTACT) is True
    assert any(
        "contact_failed_after_possible_click" in p["details"]
        for p in module.store.active_pauses()
    )


def test_main_pauses_when_managed_browser_not_running(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    opened = []

    async def fail_open_html(url, *, settle_seconds):
        opened.append(url)
        raise AssertionError("must not open a page without a managed browser")

    class FakeStatus:
        running = False
        detail = "CDP port is serving an unmanaged browser; refusing to use it"
        cdp_url = "http://127.0.0.1:9222"

    class FakeManager:
        def __init__(self, _paths):
            self.cdp_host = "127.0.0.1"
            self.cdp_url = "http://127.0.0.1:9222"

        def status(self):
            return FakeStatus()

    monkeypatch.setattr(
        module.AutonomousPolicy,
        "load",
        classmethod(lambda _cls, _paths: make_policy(module, tmp_path)),
    )
    monkeypatch.setattr(module, "load_profile", lambda _policy: ("Policy.", ["Kubernetes"]))
    monkeypatch.setattr(module, "BrowserManager", FakeManager)
    monkeypatch.setattr(module, "open_html", fail_open_html)

    rc = asyncio.run(module.main())

    assert rc == 2
    assert opened == []
    assert any(
        "managed_browser_not_running" in p["details"] for p in module.store.active_pauses()
    )


def test_main_refuses_non_loopback_cdp_host(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    opened = []

    async def fail_open_html(url, *, settle_seconds):
        opened.append(url)
        raise AssertionError("must not open a page when the CDP host is non-loopback")

    class FakeManager:
        def __init__(self, _paths):
            self.cdp_host = "0.0.0.0"
            self.cdp_url = "http://0.0.0.0:9222"

        def status(self):
            raise AssertionError("status must not be probed once a non-loopback host is rejected")

    monkeypatch.setattr(
        module.AutonomousPolicy,
        "load",
        classmethod(lambda _cls, _paths: make_policy(module, tmp_path)),
    )
    monkeypatch.setattr(module, "load_profile", lambda _policy: ("Policy.", ["Kubernetes"]))
    monkeypatch.setattr(module, "BrowserManager", FakeManager)
    monkeypatch.setattr(module, "open_html", fail_open_html)

    rc = asyncio.run(module.main())

    assert rc == 2
    assert opened == []
    assert any("cdp_host_not_loopback" in p["details"] for p in module.store.active_pauses())


class _RunningManager:
    def __init__(self, _paths):
        self.cdp_host = "127.0.0.1"
        self.cdp_url = "http://127.0.0.1:9222"

    def status(self):
        return SimpleNamespace(
            running=True, cdp_url=self.cdp_url, detail="ws://target", pid=1, browser="Chrome"
        )


async def _search_open_html(_url, *, settle_seconds):
    html = '<a class="job-name" href="/job_detail/p1.html">Role 30-50K</a>'
    return SimpleNamespace(web_socket_debugger_url="ws://target"), html, "Role listing"


def _arrange_main_run(module, monkeypatch, tmp_path, fake_process_job):
    monkeypatch.setattr(
        module.AutonomousPolicy,
        "load",
        classmethod(lambda _cls, _paths: make_policy(module, tmp_path)),
    )
    monkeypatch.setattr(module, "load_profile", lambda _policy: ("Policy.", ["Kubernetes"]))
    monkeypatch.setattr(module, "BrowserManager", _RunningManager)
    monkeypatch.setattr(module, "open_html", _search_open_html)
    monkeypatch.setattr(module, "process_job", fake_process_job)


def test_main_returns_nonzero_when_a_job_pauses(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_process_job(list_job, *, policy, profile_summary):
        module.pause("page_risk_on_detail", title=list_job.title, url=list_job.detail_url)
        return False

    _arrange_main_run(module, monkeypatch, tmp_path, fake_process_job)

    rc = asyncio.run(module.main())

    # A paused per-job stop must exit nonzero so a cron/systemd runner alerts
    # instead of treating an unfinished, manual-takeover run as success.
    assert rc == 2
    assert any("page_risk_on_detail" in p["details"] for p in module.store.active_pauses())


def test_main_returns_zero_when_cap_stops_run(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)

    async def fake_process_job(_list_job, *, policy, profile_summary):
        # Cap reached: stop the run without pausing (no manual takeover needed).
        return False

    _arrange_main_run(module, monkeypatch, tmp_path, fake_process_job)

    rc = asyncio.run(module.main())

    assert rc == 0
    assert module.store.active_pauses() == []


def _cdp_contact_eval(detail_url, events):
    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return events
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=x",
            "x": 1,
            "y": 2,
        }

    return fake_cdp_evaluate


def _arrange_cdp_contact(module, monkeypatch, detail_url, events):
    async def fake_sleep(_seconds):
        return None

    page_text_reads = []

    async def fake_page_text(_target):
        page_text_reads.append(1)
        # First read is the pre-click baseline (contact button only, no success
        # marker); later reads show the conversation marker that newly appears.
        if len(page_text_reads) == 1:
            return "Engineer role\n立即沟通"
        return "Engineer role\n继续沟通"

    async def fake_bring_to_front(*_args):
        return None

    async def fake_click(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", _cdp_contact_eval(detail_url, events))
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)


def test_cdp_contact_not_verified_without_trusted_contact_click(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    # A success marker is present, but the only trusted click hit a non-contact control.
    _arrange_cdp_contact(
        module,
        monkeypatch,
        detail_url,
        [{"type": "click", "trusted": True, "target": "A.btn btn-interest", "text": "感兴趣"}],
    )

    details = asyncio.run(
        module.click_immediate_contact(
            target, platform_job_id="boss:example", detail_url=detail_url
        )
    )

    assert details["post_click_verified"] is False
    assert details["needs_manual_verification"] is True


def test_cdp_contact_verified_with_trusted_contact_click(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")
    _arrange_cdp_contact(
        module,
        monkeypatch,
        detail_url,
        [{"type": "click", "trusted": True, "target": "div.btn-startchat", "text": "立即沟通"}],
    )

    details = asyncio.run(
        module.click_immediate_contact(
            target, platform_job_id="boss:example", detail_url=detail_url
        )
    )

    assert details["post_click_verified"] is True
    assert details["needs_manual_verification"] is False


def test_compact_profile_includes_risk_flags_to_watch(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    policy = make_policy(module, tmp_path)
    data = {
        "candidate_positioning": "Senior SRE",
        "risk_flags_to_watch": ["unverified compensation", "vague equity"],
        "autonomous_policy": {"mode": "test"},
    }

    compacted = module.compact_profile_for_decision(data, policy)

    # The candidate-specific risk conditions must reach the compacted LLM profile.
    assert compacted["risk_flags_to_watch"] == ["unverified compensation", "vague equity"]


def test_cdp_precheck_failure_is_pre_click_abort(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        return {}

    async def fake_page_text(_target):
        raise RuntimeError("CDP target detached")

    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "page_text", fake_page_text)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            module.click_immediate_contact(
                target, platform_job_id="boss:example", detail_url=detail_url
            )
        )

    # A stale CDP read before the click must be tagged as a pre-click abort so
    # process_job releases the reservation instead of confirming a phantom contact.
    assert str(exc_info.value).startswith(module.PRE_CLICK_ABORT_PREFIXES)


def test_playwright_fallback_confirms_without_already_in_conversation(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")

    async def fake_sleep(_seconds):
        return None

    async def fake_page_text(_target):
        return "Senior platform engineer\n立即沟通"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return []
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=ok",
            "x": 1,
            "y": 2,
        }

    async def fake_bring_to_front(*_args):
        return None

    async def fake_click(*_args, **_kwargs):
        return None

    async def fake_playwright_click(_url):
        return {
            "ok": True,
            "reason": "already_in_conversation",
            "clicked_label": "继续沟通",
            "post_click_verified": True,
            "post_click_risks": [],
        }

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "page_text", fake_page_text)
    monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
    monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
    monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_click)
    monkeypatch.setattr(module, "playwright_click_immediate_contact", fake_playwright_click)

    details = asyncio.run(
        module.click_immediate_contact(
            target, platform_job_id="boss:example", detail_url=detail_url
        )
    )

    # This fallback runs after the CDP click was dispatched, so a "继续沟通" it sees
    # may be the result of that click. The contact is confirmed (post_click_verified)
    # rather than released, and the already_in_conversation release flag is not set —
    # otherwise a later run could message the same recruiter again.
    assert details["post_click_verified"] is True
    assert details.get("already_in_conversation") is not True


def test_prior_llm_timeout_does_not_permanently_skip(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    job = make_job(module)
    job_id, _ = module.store.upsert_job(job)
    module.record_skip(job_id, "llm_decision_timeout", confidence=0.0)

    contacted = []

    async def fake_click(*_args, **_kwargs):
        contacted.append(True)
        return {
            "clicked_label": "立即沟通",
            "post_click_verified": True,
            "needs_manual_verification": False,
        }

    monkeypatch.setattr(module, "client", _apply_client(module))
    monkeypatch.setattr(module, "open_html", _detail_open_html)
    monkeypatch.setattr(module, "click_immediate_contact", fake_click)

    keep_going = asyncio.run(
        module.process_job(job, policy=make_policy(module, tmp_path), profile_summary="Policy.")
    )

    # A single prior timeout must not exclude the job forever; it is re-evaluated
    # and contacted on a later run.
    assert contacted == [True]
    assert keep_going is True


def test_main_refuses_to_run_with_unresolved_pause(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    module.store.pause("page_risk_on_search", details={"keyword": "k8s"})

    async def fail_open_html(_url, *, settle_seconds):
        raise AssertionError("must not open pages while a pause is unresolved")

    monkeypatch.setattr(
        module.AutonomousPolicy,
        "load",
        classmethod(lambda _cls, _paths: make_policy(module, tmp_path)),
    )
    monkeypatch.setattr(module, "load_profile", lambda _policy: ("Policy.", ["Kubernetes"]))
    monkeypatch.setattr(module, "open_html", fail_open_html)

    rc = asyncio.run(module.main())

    # A new run must not proceed while a previous pause awaits manual takeover.
    assert rc == 2


def test_cdp_contact_search_is_scoped_to_selected_detail_box(monkeypatch, tmp_path):
    import inspect

    module = load_remote_module(monkeypatch, tmp_path)
    source = inspect.getsource(module.click_immediate_contact)

    # The in-browser contact search must be scoped to the selected job's box so a
    # recommendation card's control cannot be borrowed (mirrors the HTML adapter).
    assert ".job-detail-box" in source
    assert "scope.querySelectorAll" in source
    assert "document.querySelectorAll" not in source


def test_cdp_dispatch_failure_distinguishes_pre_and_post_dispatch(monkeypatch, tmp_path):
    module = load_remote_module(monkeypatch, tmp_path)
    detail_url = "https://www.zhipin.com/job_detail/example.html"
    target = SimpleNamespace(web_socket_debugger_url="ws://target")

    async def fake_sleep(_seconds):
        return None

    async def fake_page_text(_target):
        return "Engineer role"

    async def fake_cdp_evaluate(_web_socket_url, expression):
        if expression == "window.location.href":
            return detail_url
        if expression == "window.__resumePilotClickEvents || []":
            return []
        return {
            "ok": True,
            "reason": "clickable_center_found",
            "count": 1,
            "redirect_url": "/web/geek/chat?id=ok",
            "x": 1,
            "y": 2,
        }

    async def fake_bring_to_front(*_args):
        return None

    def arrange(*, dispatched):
        async def fake_dispatch(*_args, **_kwargs):
            raise module.CdpClickError("socket dropped", dispatched=dispatched)

        monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(module, "page_text", fake_page_text)
        monkeypatch.setattr(module, "cdp_evaluate", fake_cdp_evaluate)
        monkeypatch.setattr(module, "cdp_bring_to_front", fake_bring_to_front)
        monkeypatch.setattr(module, "cdp_dispatch_mouse_click", fake_dispatch)

    # No mouse event was sent -> pre-click abort that releases the reservation.
    arrange(dispatched=False)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            module.click_immediate_contact(
                target, platform_job_id="boss:example", detail_url=detail_url
            )
        )
    assert not isinstance(exc_info.value, module.CdpClickError)
    assert str(exc_info.value).startswith("contact_button_not_safe")

    # A mouse event was sent -> the click may have landed -> propagate to confirm.
    arrange(dispatched=True)
    with pytest.raises(module.CdpClickError):
        asyncio.run(
            module.click_immediate_contact(
                target, platform_job_id="boss:example", detail_url=detail_url
            )
        )
