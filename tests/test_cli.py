from __future__ import annotations

import json

import pytest

from resume_pilot.boss import HumanPauseRequired
from resume_pilot.cli import _validate_boss_source_url, _wait_for_live_page_html, main


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
