from __future__ import annotations

import stat

from resume_pilot.config import ensure_private_dir, ensure_private_parent


def test_ensure_private_dir_locks_down_existing_loose_directory(tmp_path):
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    profile.chmod(0o755)

    ensure_private_dir(profile)

    assert stat.S_IMODE(profile.stat().st_mode) == 0o700


def test_ensure_private_parent_leaves_existing_external_directory_untouched(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    external.chmod(0o755)

    ensure_private_parent(external / "state.sqlite")

    assert stat.S_IMODE(external.stat().st_mode) == 0o755


def test_ensure_private_parent_secures_directories_it_creates(tmp_path):
    db = tmp_path / "fresh-private" / "state.sqlite"

    ensure_private_parent(db)

    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
