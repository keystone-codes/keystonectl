"""Packaging and release helper checks."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

from keystonectl.cli import main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXTRACT_PATH = _REPO_ROOT / "scripts" / "extract_changelog.py"

_spec = importlib.util.spec_from_file_location("extract_changelog", _EXTRACT_PATH)
assert _spec is not None and _spec.loader is not None
extract_changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_changelog)


def test_version_flag_matches_pyproject(capsys) -> None:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = data["project"]["version"]
    assert main(["--version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"keystonectl {expected}"


def test_extract_changelog_prints_section() -> None:
    text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = extract_changelog.extract_section(text, "Unreleased")
    assert section is not None
    assert "trusted publishing" in section
