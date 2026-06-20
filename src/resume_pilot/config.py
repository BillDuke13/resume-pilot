from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "resume-pilot"
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222
DEFAULT_DAILY_CAP = 150
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CLAUDE_MODEL = "kimi-k2.7-code"


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def default_data_dir() -> Path:
    return _path_from_env("RESUME_PILOT_DATA_DIR") or Path.home() / ".local/share" / APP_NAME


def default_state_dir() -> Path:
    return _path_from_env("RESUME_PILOT_STATE_DIR") or Path.home() / ".local/state" / APP_NAME


def default_state_db_path() -> Path:
    return _path_from_env("RESUME_PILOT_STATE_DB") or default_data_dir() / "state.sqlite"


def default_chrome_profile_dir() -> Path:
    return (
        _path_from_env("RESUME_PILOT_CHROME_PROFILE")
        or default_state_dir() / "chrome-profile"
    )


def default_profile_cache_path() -> Path:
    return _path_from_env("RESUME_PILOT_PROFILE_CACHE") or default_data_dir() / "profile.json"


def default_pid_path() -> Path:
    return default_state_dir() / "browser.pid"


def default_browser_log_path() -> Path:
    return default_state_dir() / "browser.log"


def default_cdp_host() -> str:
    return os.environ.get("RESUME_PILOT_CDP_HOST", DEFAULT_CDP_HOST)


def default_cdp_port() -> int:
    return int(os.environ.get("RESUME_PILOT_CDP_PORT", str(DEFAULT_CDP_PORT)))


def default_cdp_url() -> str:
    return f"http://{default_cdp_host()}:{default_cdp_port()}"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    return path


def ensure_private_parent(path: Path) -> Path:
    ensure_private_dir(path.parent)
    return path


@dataclass(frozen=True)
class AppPaths:
    state_db: Path
    state_dir: Path
    data_dir: Path
    chrome_profile: Path
    profile_cache: Path
    browser_pid: Path
    browser_log: Path

    @classmethod
    def defaults(cls) -> AppPaths:
        data_dir = default_data_dir()
        state_dir = default_state_dir()
        return cls(
            state_db=default_state_db_path(),
            state_dir=state_dir,
            data_dir=data_dir,
            chrome_profile=default_chrome_profile_dir(),
            profile_cache=default_profile_cache_path(),
            browser_pid=default_pid_path(),
            browser_log=default_browser_log_path(),
        )

    def ensure_private(self) -> None:
        ensure_private_dir(self.data_dir)
        ensure_private_dir(self.state_dir)
        ensure_private_dir(self.chrome_profile)
        ensure_private_parent(self.state_db)
        ensure_private_parent(self.profile_cache)
        ensure_private_parent(self.browser_pid)
        ensure_private_parent(self.browser_log)
