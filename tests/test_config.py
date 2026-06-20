from __future__ import annotations

import stat

from resume_pilot.config import ensure_private_dir


def test_ensure_private_dir_locks_down_existing_loose_directory(tmp_path):
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    profile.chmod(0o755)

    ensure_private_dir(profile)

    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
