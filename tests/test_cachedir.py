"""``.keystonectl/`` is created with mode 700 and a one-line ``.gitignore``."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from keystonectl.cachedir import CACHE_GITIGNORE, ensure_cache_dir


def test_ensure_cache_dir_on_bare_root(tmp_path: Path) -> None:
    base = ensure_cache_dir(tmp_path)
    assert base.is_dir()
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    gitignore = base / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == CACHE_GITIGNORE
    assert stat.S_IMODE(gitignore.stat().st_mode) == 0o644


def test_ensure_cache_dir_adds_gitignore_when_missing(tmp_path: Path) -> None:
    base = tmp_path / ".keystonectl"
    base.mkdir()
    os.chmod(base, 0o700)
    ensure_cache_dir(tmp_path)
    assert (base / ".gitignore").read_text(encoding="utf-8") == CACHE_GITIGNORE


def test_ensure_cache_dir_tightens_permissions(tmp_path: Path) -> None:
    base = tmp_path / ".keystonectl"
    base.mkdir()
    os.chmod(base, 0o755)
    ensure_cache_dir(tmp_path)
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


def test_ensure_cache_dir_keeps_existing_gitignore(tmp_path: Path) -> None:
    base = tmp_path / ".keystonectl"
    base.mkdir()
    os.chmod(base, 0o700)
    gitignore = base / ".gitignore"
    gitignore.write_text("keep\n", encoding="utf-8")
    ensure_cache_dir(tmp_path)
    assert gitignore.read_text(encoding="utf-8") == "keep\n"
