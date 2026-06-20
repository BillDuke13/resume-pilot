from __future__ import annotations

import os
import signal

from resume_pilot.browser import BrowserManager
from resume_pilot.config import AppPaths


def _paths(tmp_path):
    return AppPaths(
        state_db=tmp_path / "state.sqlite",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        chrome_profile=tmp_path / "chrome-profile",
        profile_cache=tmp_path / "profile.json",
        browser_pid=tmp_path / "browser.pid",
        browser_log=tmp_path / "browser.log",
    )


def test_stop_skips_kill_when_pid_is_not_managed_browser(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.browser_pid.write_text("4242", encoding="utf-8")
    manager = BrowserManager(paths)

    monkeypatch.setattr("resume_pilot.browser.fetch_cdp_version", lambda *_a, **_k: None)
    monkeypatch.setattr(BrowserManager, "_pid_is_managed_browser", lambda _self, _pid: False)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    manager.stop()

    assert killed == []
    assert not paths.browser_pid.exists()


def test_stop_signals_managed_browser(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.browser_pid.write_text("4242", encoding="utf-8")
    manager = BrowserManager(paths)

    monkeypatch.setattr("resume_pilot.browser.fetch_cdp_version", lambda *_a, **_k: None)
    monkeypatch.setattr(BrowserManager, "_pid_is_managed_browser", lambda _self, _pid: True)
    monkeypatch.setattr(BrowserManager, "_pid_alive", staticmethod(lambda _pid: False))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    manager.stop()

    assert killed == [(4242, signal.SIGTERM)]
    assert not paths.browser_pid.exists()


def test_status_rejects_unmanaged_cdp_browser(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    manager = BrowserManager(paths)

    monkeypatch.setattr(
        "resume_pilot.browser.fetch_cdp_version", lambda *_a, **_k: {"Browser": "Chrome/1"}
    )
    monkeypatch.setattr(BrowserManager, "_pid_is_managed_browser", lambda _self, _pid: False)

    status = manager.status()

    assert status.running is False
    assert "unmanaged" in (status.detail or "")


def test_status_accepts_managed_cdp_browser(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.browser_pid.write_text("4242", encoding="utf-8")
    manager = BrowserManager(paths)

    monkeypatch.setattr(
        "resume_pilot.browser.fetch_cdp_version",
        lambda *_a, **_k: {"Browser": "Chrome/1", "webSocketDebuggerUrl": "ws://x"},
    )
    monkeypatch.setattr(BrowserManager, "_pid_is_managed_browser", lambda _self, _pid: True)

    status = manager.status()

    assert status.running is True


def test_browser_manager_honors_cdp_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_PILOT_CDP_HOST", "127.0.0.1")
    monkeypatch.setenv("RESUME_PILOT_CDP_PORT", "9333")

    manager = BrowserManager(_paths(tmp_path))

    assert manager.cdp_port == 9333
    assert manager.cdp_url == "http://127.0.0.1:9333"
