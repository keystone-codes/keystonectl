"""``.keystonectl/`` layout: private mode and a single-line ``.gitignore``."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_GITIGNORE = "*\n"
PRIVATE_DIR_MODE = 0o700
GITIGNORE_MODE = 0o644


def _tighten(path: Path, *, created: bool) -> None:
    if created or (path.stat().st_mode & 0o077):
        os.chmod(path, PRIVATE_DIR_MODE)


def ensure_cache_dir(root: Path) -> Path:
    base = Path(root) / ".keystonectl"
    created = not base.is_dir()
    base.mkdir(parents=True, exist_ok=True)
    _tighten(base, created=created)
    gitignore = base / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(CACHE_GITIGNORE)
        os.chmod(gitignore, GITIGNORE_MODE)
    return base


def private_subdir(root: Path, name: str) -> Path:
    base = ensure_cache_dir(root)
    sub = base / name
    created = not sub.is_dir()
    sub.mkdir(parents=True, exist_ok=True)
    _tighten(sub, created=created)
    return sub
