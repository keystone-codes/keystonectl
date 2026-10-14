"""Packaging and release helper checks."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

from keystonectl.cli import main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXTRACT_PATH = _REPO_ROOT / "scripts" / "extract_changelog.py"
_CHECK_VERSION_PATH = _REPO_ROOT / "scripts" / "check_version.py"

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
    section = extract_changelog.extract_section(text, "0.1.0")
    assert section is not None
    assert "trusted publishing" in section


def test_check_version_passes_for_release_tag() -> None:
    result = subprocess.run(
        [sys.executable, str(_CHECK_VERSION_PATH), "--tag", "v0.1.0"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme_has_no_relative_repo_links() -> None:
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "](docs/" not in text
    assert "](skills/" not in text
    assert "](LICENSE)" not in text


def test_security_md_exists_and_lists_supported_versions() -> None:
    text = (_REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Supported versions" in text
    assert "0.1.x" in text
