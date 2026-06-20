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
