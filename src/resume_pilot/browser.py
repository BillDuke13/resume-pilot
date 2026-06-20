from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from resume_pilot.config import (
    DEFAULT_CDP_HOST,
    DEFAULT_CDP_PORT,
    AppPaths,
    default_cdp_url,
)

BROWSER_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


@dataclass(frozen=True)
class BrowserStatus:
    running: bool
    cdp_url: str
    pid: int | None = None
    browser: str | None = None
    detail: str | None = None


def find_browser_binary() -> str | None:
    env_value = os.environ.get("RESUME_PILOT_CHROME_BIN")
    if env_value:
        return env_value
    for candidate in BROWSER_CANDIDATES:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def fetch_cdp_version(cdp_url: str, *, timeout_seconds: float = 2.0) -> dict[str, str] | None:
    try:
        version_url = f"{cdp_url.rstrip('/')}/json/version"
        with urllib.request.urlopen(version_url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


class BrowserManager:
    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        cdp_host: str = DEFAULT_CDP_HOST,
        cdp_port: int = DEFAULT_CDP_PORT,
    ):
        self.paths = paths or AppPaths.defaults()
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.cdp_url = f"http://{cdp_host}:{cdp_port}"

    def status(self) -> BrowserStatus:
        version = fetch_cdp_version(self.cdp_url)
        pid = self._read_pid()
        if version:
            if pid is None or not self._pid_is_managed_browser(pid):
                return BrowserStatus(
                    running=False,
                    cdp_url=self.cdp_url,
                    pid=pid,
                    browser=version.get("Browser"),
                    detail="CDP port is serving an unmanaged browser; refusing to use it",
                )
            return BrowserStatus(
                running=True,
                cdp_url=self.cdp_url,
                pid=pid,
                browser=version.get("Browser"),
                detail=version.get("webSocketDebuggerUrl"),
            )
        return BrowserStatus(running=False, cdp_url=self.cdp_url, pid=pid)

    def start(self) -> BrowserStatus:
        self.paths.ensure_private()
        current = self.status()
        if current.running:
            return current
        if fetch_cdp_version(self.cdp_url) is not None:
            return BrowserStatus(
                running=False,
                cdp_url=self.cdp_url,
                detail="CDP port is occupied by an unmanaged browser; not starting a new one",
            )

        binary = find_browser_binary()
        if not binary:
            return BrowserStatus(
                running=False,
                cdp_url=self.cdp_url,
                detail="No Chromium or Chrome binary found on PATH",
            )

        env = os.environ.copy()
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":1"
        log_file = self.paths.browser_log.open("ab")
        process = subprocess.Popen(
            [
                binary,
                f"--remote-debugging-address={self.cdp_host}",
                f"--remote-debugging-port={self.cdp_port}",
                f"--user-data-dir={self.paths.chrome_profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.paths.browser_pid.write_text(str(process.pid), encoding="utf-8")

        for _ in range(30):
            status = self.status()
            if status.running:
                return status
            time.sleep(0.5)
        return BrowserStatus(
            running=False,
            cdp_url=self.cdp_url,
            pid=process.pid,
            detail=f"Browser process started but CDP did not answer; see {self.paths.browser_log}",
        )

    def stop(self) -> BrowserStatus:
        pid = self._read_pid()
        if pid is None:
            return self.status()
        if not self._pid_is_managed_browser(pid):
            self.paths.browser_pid.unlink(missing_ok=True)
            return self.status()
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not self._pid_alive(pid):
                self.paths.browser_pid.unlink(missing_ok=True)
                return self.status()
            time.sleep(0.25)
        return BrowserStatus(
            running=True,
            cdp_url=self.cdp_url,
            pid=pid,
            detail="Browser did not stop after SIGTERM",
        )

    def _read_pid(self) -> int | None:
        try:
            text = self.paths.browser_pid.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return int(text) if text.isdigit() else None

    def _pid_is_managed_browser(self, pid: int) -> bool:
        """Confirm the stored PID still belongs to this profile's browser.

        Guards against killing an unrelated process that reused a stale PID. The
        check reads ``/proc/<pid>/cmdline`` and requires the managed profile path
        to appear in the launch arguments. When the command line cannot be read
        (process gone, no permission, or no procfs), it is treated as not managed,
        so the caller clears the stale pid file instead of sending a signal.
        """
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            return False
        arguments = cmdline.decode("utf-8", errors="replace")
        return str(self.paths.chrome_profile) in arguments

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def connect_existing_browser(cdp_url: str | None = None):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(cdp_url or default_cdp_url())
    return playwright, browser
