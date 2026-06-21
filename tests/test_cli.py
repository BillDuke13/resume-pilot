from __future__ import annotations

import json

import pytest

from resume_pilot.boss import BossHtmlAdapter, HumanPauseRequired
from resume_pilot.cli import (
    _click_unique_live_contact,
    _validate_boss_source_url,
    _wait_for_live_page_html,
    main,
)


class FakePage:
    def __init__(self, html_snapshots: list[str]):
        self.html_snapshots = html_snapshots
        self.waits = 0

    def content(self) -> str:
        if len(self.html_snapshots) > 1:
            return self.html_snapshots.pop(0)
        return self.html_snapshots[0]

    def wait_for_timeout(self, _milliseconds: int) -> None:
        self.waits += 1


def test_inbox_watch_parses_fixture(capsys):
    exit_code = main(
        [
            "inbox",
            "watch",
            "--dry-run",
            "--html-file",
            "ops/fixtures/boss_inbox.html",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "inspected"
    assert output["candidate_replies"] == 1
    assert output["candidates"][0]["conversation_id"] == "conversation-fixture-1"


def test_live_html_reader_waits_for_boss_ready_marker():
    page = FakePage(
        [
            "<html><body>loading</body></html>",
            "<html><body><a href='/job_detail/example.html'>Role</a></body></html>",
        ]
    )

    html = _wait_for_live_page_html(page)

    assert "/job_detail/example.html" in html
    assert page.waits == 1


def test_live_execute_source_url_must_be_boss_job_url():
    _validate_boss_source_url("https://www.zhipin.com/web/geek/jobs")
    _validate_boss_source_url("https://www.zhipin.com/job_detail/example.html")

    with pytest.raises(HumanPauseRequired):
        _validate_boss_source_url("http://www.zhipin.com/web/geek/jobs")

    with pytest.raises(HumanPauseRequired):
        _validate_boss_source_url("https://example.com/web/geek/jobs")


def test_live_execute_rejects_decision_fixture(tmp_path, capsys):
    exit_code = main(
        [
            "--state-db",
            str(tmp_path / "state.sqlite"),
            "run",
            "--execute",
            "--confirm-live-contact",
            "--decision-fixture",
            "apply",
            "--source-url",
            "https://www.zhipin.com/web/geek/jobs",
        ]
    )

    assert exit_code == 2
    assert "--decision-fixture" in capsys.readouterr().err


def test_browser_stop_returns_nonzero_when_browser_survives(tmp_path, monkeypatch):
    from resume_pilot.browser import BrowserManager, BrowserStatus

    def fake_stop(_self):
        return BrowserStatus(
            running=True,
            cdp_url="http://127.0.0.1:9222",
            detail="Browser did not stop after SIGTERM",
        )

    monkeypatch.setattr(BrowserManager, "stop", fake_stop)

    exit_code = main(["--state-db", str(tmp_path / "state.sqlite"), "browser", "stop"])

    assert exit_code == 1


def test_live_execute_requires_managed_browser(tmp_path, monkeypatch, capsys):
    from resume_pilot.browser import BrowserManager, BrowserStatus

    monkeypatch.setattr(
        BrowserManager,
        "status",
        lambda _self: BrowserStatus(
            running=False,
            cdp_url="http://127.0.0.1:9222",
            detail="CDP port is serving an unmanaged browser; refusing to use it",
        ),
    )

    exit_code = main(
        [
            "--state-db",
            str(tmp_path / "state.sqlite"),
            "run",
            "--execute",
            "--confirm-live-contact",
            "--source-url",
            "https://www.zhipin.com/web/geek/jobs",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["paused"] is True
    assert "managed_browser_not_running" in output["reason"]


def test_load_profile_summary_reads_private_cache(tmp_path):
    from resume_pilot.cli import _load_profile_summary
    from resume_pilot.config import AppPaths

    cache = tmp_path / "profile.json"
    cache.write_text(json.dumps({"text": "Private candidate policy."}), encoding="utf-8")
    paths = AppPaths(
        state_db=tmp_path / "state.sqlite",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        chrome_profile=tmp_path / "chrome-profile",
        profile_cache=cache,
        browser_pid=tmp_path / "browser.pid",
        browser_log=tmp_path / "browser.log",
    )

    assert _load_profile_summary(paths, None) == "Private candidate policy."


def test_load_profile_summary_returns_none_without_cache_or_file(tmp_path):
    from resume_pilot.cli import _load_profile_summary
    from resume_pilot.config import AppPaths

    paths = AppPaths(
        state_db=tmp_path / "state.sqlite",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        chrome_profile=tmp_path / "chrome-profile",
        profile_cache=tmp_path / "missing-profile.json",
        browser_pid=tmp_path / "browser.pid",
        browser_log=tmp_path / "browser.log",
    )

    assert _load_profile_summary(paths, None) is None


def test_load_profile_summary_raises_when_explicit_file_missing(tmp_path):
    from resume_pilot.cli import _load_profile_summary
    from resume_pilot.config import AppPaths

    paths = AppPaths(
        state_db=tmp_path / "state.sqlite",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        chrome_profile=tmp_path / "chrome-profile",
        profile_cache=tmp_path / "cache.json",
        browser_pid=tmp_path / "browser.pid",
        browser_log=tmp_path / "browser.log",
    )

    with pytest.raises(OSError):
        _load_profile_summary(paths, tmp_path / "does-not-exist.txt")


def test_run_over_saved_html_fixture_does_not_crash(tmp_path, capsys):
    exit_code = main(
        [
            "--state-db",
            str(tmp_path / "state.sqlite"),
            "run",
            "--dry-run",
            "--decision-fixture",
            "skip",
            "--source-url",
            "https://www.zhipin.com/web/geek/jobs",
            "--html-file",
            "ops/fixtures/boss_jobs.html",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert "discovered" in output


def test_live_execute_rejects_non_loopback_cdp_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESUME_PILOT_CDP_HOST", "0.0.0.0")

    exit_code = main(
        [
            "--state-db",
            str(tmp_path / "state.sqlite"),
            "run",
            "--execute",
            "--confirm-live-contact",
            "--source-url",
            "https://www.zhipin.com/web/geek/jobs",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["paused"] is True
    assert "cdp_host_not_loopback" in output["reason"]
    # Pause details are structured JSON (not a stringified Python repr).
    assert output["details"]["cdp_host"] == "0.0.0.0"


_LIVE_CONTACT_DETAIL_HTML = (
    "<div class='job-detail-container'><div class='job-detail-box'>"
    "<div class='job-detail-header'>"
    "<div class='job-detail-info'>Test Engineer 30-50K</div>"
    "<div class='job-detail-op'><a class='op-btn'>立即沟通</a></div>"
    "</div><div class='job-detail-body'>职位描述 Python</div></div></div>"
)


class _FakeButton:
    def __init__(self, *, visible: bool = True, raise_on_click: bool = False, trial_error=None):
        self._visible = visible
        self._raise_on_click = raise_on_click
        self._trial_error = trial_error
        self.clicked = False

    def is_visible(self, timeout=None) -> bool:
        return self._visible

    def evaluate(self, _expression) -> bool:
        return False

    def click(self, timeout=None, trial: bool = False) -> None:
        if trial:
            if self._trial_error is not None:
                raise self._trial_error
            return
        if self._raise_on_click:
            raise RuntimeError("element is not clickable: detached from DOM")
        self.clicked = True


class _FakeSelectorLocator:
    def __init__(self, items, *, count_error: Exception | None = None):
        self._items = items
        self._count_error = count_error

    def count(self) -> int:
        if self._count_error is not None:
            raise self._count_error
        return len(self._items)

    def nth(self, index):
        return self._items[index]


class _FakeBodyLocator:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self, timeout=None) -> str:
        return self._text


class FakeContactPage:
    def __init__(
        self,
        *,
        post_html: str,
        buttons,
        body_text: str,
        url: str,
        scan_error=None,
        before_body_text: str = "",
    ):
        self._snapshots = [_LIVE_CONTACT_DETAIL_HTML, post_html]
        self.url = url
        self._buttons = buttons
        self._before_body_text = before_body_text
        self._post_body_text = body_text
        self._body_reads = 0
        self._scan_error = scan_error
        self.locator_selectors: list[str] = []
        self.waits = 0

    def content(self) -> str:
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def wait_for_timeout(self, _milliseconds: int) -> None:
        self.waits += 1

    def locator(self, selector: str, has_text=None):
        self.locator_selectors.append(selector)
        if selector == "body":
            text = self._before_body_text if self._body_reads == 0 else self._post_body_text
            self._body_reads += 1
            return _FakeBodyLocator(text)
        return _FakeSelectorLocator(self._buttons, count_error=self._scan_error)


def test_live_contact_locator_includes_role_buttons():
    page = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话 继续沟通</div>",
        buttons=[_FakeButton(visible=True)],
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
    )

    result = _click_unique_live_contact(page, BossHtmlAdapter(), "test123")

    # The adapter accepts role="button" controls in button_labels(); the live click
    # locator must scan the same shapes (scoped to the selected box) so an accepted
    # control is actually clicked.
    assert (
        ".job-detail-box a, .job-detail-box button, .job-detail-box [role=button]"
        in page.locator_selectors
    )
    assert result["job_id"] == "test123"


def test_live_contact_success_requires_newly_appearing_marker():
    # Marker only in hidden raw HTML (not the visible body) -> not verified.
    hidden_marker = FakeContactPage(
        post_html="<div class='job-detail'><script>继续沟通</script></div>",
        buttons=[_FakeButton(visible=True)],
        before_body_text="立即沟通",
        body_text="对话尚未开始",
        url="https://www.zhipin.com/job_detail/test123.html",
    )
    hidden = _click_unique_live_contact(hidden_marker, BossHtmlAdapter(), "test123")
    assert hidden["post_click_verified"] is False
    assert hidden["needs_manual_verification"] is True

    # Marker present before AND after the click (site nav) -> not newly appearing.
    nav_marker = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[_FakeButton(visible=True)],
        before_body_text="继续沟通",
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
    )
    nav = _click_unique_live_contact(nav_marker, BossHtmlAdapter(), "test123")
    assert nav["post_click_verified"] is False
    assert nav["needs_manual_verification"] is True

    # A marker that appears only after the click confirms the contact.
    transitioned = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[_FakeButton(visible=True)],
        before_body_text="立即沟通",
        body_text="继续沟通 发送简历",
        url="https://www.zhipin.com/job_detail/test123.html",
    )
    confirmed = _click_unique_live_contact(transitioned, BossHtmlAdapter(), "test123")
    assert confirmed["post_click_verified"] is True
    assert confirmed["needs_manual_verification"] is False


def test_live_contact_trial_failure_is_pre_click_abort():
    page = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[_FakeButton(visible=True, trial_error=RuntimeError("not actionable"))],
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
    )

    # The control never becomes actionable, so the trial click fails before any
    # mouse event — a pre-click abort the runner releases.
    with pytest.raises(HumanPauseRequired) as exc_info:
        _click_unique_live_contact(page, BossHtmlAdapter(), "test123")
    assert exc_info.value.reason == "contact_button_unclickable"


def test_pauses_list_and_resolve(tmp_path, capsys):
    from resume_pilot.state import StateStore

    db = tmp_path / "state.sqlite"
    StateStore(db).pause("page_risk_on_search", details={"keyword": "k8s"})

    assert main(["--state-db", str(db), "pauses", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["active_pauses"]) == 1
    assert listed["active_pauses"][0]["reason"] == "page_risk_on_search"
    assert listed["active_pauses"][0]["details"] == {"keyword": "k8s"}

    assert main(["--state-db", str(db), "pauses", "resolve"]) == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == 1

    assert main(["--state-db", str(db), "pauses", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["active_pauses"] == []


def test_live_contact_unverified_when_pre_click_read_failed():
    page = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[_FakeButton(visible=True)],
        before_body_text="",
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
    )

    result = _click_unique_live_contact(page, BossHtmlAdapter(), "test123")

    # An empty pre-click read gives no baseline, so a marker cannot be proven newly
    # appeared and the contact stays unverified (manual-verification pause fires).
    assert result["post_click_verified"] is False
    assert result["needs_manual_verification"] is True


def test_live_execute_blocked_by_unresolved_pause(tmp_path, capsys):
    from resume_pilot.state import StateStore

    db = tmp_path / "state.sqlite"
    StateStore(db).pause("contact_click_needs_manual_verification", details={"job_id": "x"})

    exit_code = main(
        [
            "--state-db",
            str(db),
            "run",
            "--execute",
            "--confirm-live-contact",
            "--source-url",
            "https://www.zhipin.com/web/geek/jobs",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["paused"] is True
    assert output["reason"] == "unresolved_pauses_block_live_run"


def test_inbox_watch_pauses_on_security_page(tmp_path, capsys):
    html_file = tmp_path / "inbox.html"
    html_file.write_text("<html><body>请完成 安全验证 验证码</body></html>", encoding="utf-8")

    exit_code = main(["inbox", "watch", "--dry-run", "--html-file", str(html_file)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["status"] == "paused"
    assert output["reason"] == "page_risk_on_inbox"


def test_inbox_watch_live_read_persists_page_risk_pause(tmp_path, capsys, monkeypatch):
    import resume_pilot.cli as cli_module
    from resume_pilot.state import StateStore

    db = tmp_path / "state.sqlite"
    monkeypatch.setattr(
        cli_module, "_read_live_page_html", lambda _url: "<body>请完成 安全验证 验证码</body>"
    )

    exit_code = main(
        [
            "--state-db",
            str(db),
            "inbox",
            "watch",
            "--dry-run",
            "--url",
            "https://www.zhipin.com/web/geek/chat",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["reason"] == "page_risk_on_inbox"
    # A live risk page persists the pause so the next contact run's gate also blocks.
    assert any(p["reason"] == "page_risk_on_inbox" for p in StateStore(db).active_pauses())


def test_live_contact_locator_failure_is_pre_click_abort():
    page = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[],
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
        scan_error=RuntimeError("page detached during locator scan"),
    )

    # Locating/visibility checks run before any mouse event, so a failure there is
    # a pre-click abort the runner can release rather than a burned cap slot.
    with pytest.raises(HumanPauseRequired) as exc_info:
        _click_unique_live_contact(page, BossHtmlAdapter(), "test123")
    assert exc_info.value.reason == "contact_locator_unavailable"


def test_live_contact_click_failure_propagates_for_post_click_handling():
    page = FakeContactPage(
        post_html="<div class='job-detail'>已进入会话</div>",
        buttons=[_FakeButton(visible=True, raise_on_click=True)],
        body_text="继续沟通",
        url="https://www.zhipin.com/job_detail/test123.html",
    )

    # A click can throw after the mouse event is dispatched; that must NOT become a
    # pre-click abort (which would release and risk a duplicate). It propagates so
    # the runner's generic handler confirms a possible contact instead.
    with pytest.raises(RuntimeError) as exc_info:
        _click_unique_live_contact(page, BossHtmlAdapter(), "test123")
    assert not isinstance(exc_info.value, HumanPauseRequired)
