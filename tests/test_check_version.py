"""Tests for scripts/check_version.py."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_version.py"

_spec = importlib.util.spec_from_file_location("check_version", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version)


def _write_project(
    tmp_path: Path,
    *,
    pyproject_version: str = "0.1.0",
    dunder_version: str = "0.1.0",
    changelog_heading: str = "## [Unreleased]\n",
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "keystonectl"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    package_dir = tmp_path / "src" / "keystonectl"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        f'"""Demo package."""\n\n__version__ = "{dunder_version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n{changelog_heading}",
        encoding="utf-8",
    )
    return tmp_path


def _write_wheel(
    dist_dir: Path,
    *,
    version: str = "0.1.0",
    include_required_files: bool = True,
    include_entry_point: bool = True,
) -> Path:
    normalized = check_version.NORMALIZED_DIST_NAME
    name = check_version.DIST_NAME
    wheel_path = dist_dir / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        metadata = (
            "Metadata-Version: 2.4\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            "License-Expression: Apache-2.0\n"
            "Summary: Demo wheel\n"
        )
        archive.writestr(f"{normalized}-{version}.dist-info/METADATA", metadata)
        if include_required_files:
            for relative_path in check_version.REQUIRED_WHEEL_FILES:
                archive.writestr(relative_path, "{}")
        if include_entry_point:
            archive.writestr(
                f"{normalized}-{version}.dist-info/entry_points.txt",
                f"[console_scripts]\n{check_version._CONSOLE_SCRIPT_LINE}\n",
            )
    return wheel_path


def _write_sdist(
    dist_dir: Path,
    *,
    version: str = "0.1.0",
    extra_members: tuple[str, ...] = (),
) -> Path:
    normalized = check_version.NORMALIZED_DIST_NAME
    sdist_path = dist_dir / f"{normalized}-{version}.tar.gz"
    top = f"{normalized}-{version}"
    with tarfile.open(sdist_path, mode="w:gz") as archive:

        def add(relative_path: str, content: str = "x\n") -> None:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top}/{relative_path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        add("pyproject.toml")
        add("src/keystonectl/__init__.py")
        for relative_path in extra_members:
            add(relative_path)
    return sdist_path


def test_sources_agree(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    assert check_version.check(root, None) == []
    assert check_version.main(["--root", str(root)]) == 0


def test_pyproject_vs_dunder_mismatch(tmp_path: Path) -> None:
    root = _write_project(tmp_path, pyproject_version="0.1.1")
    errors = check_version.check(root, None)
    assert errors
    assert check_version.main(["--root", str(root)]) == 1


def test_unreleased_only_changelog_ok_without_tag(tmp_path: Path) -> None:
    root = _write_project(tmp_path, changelog_heading="## [Unreleased]\n")
    assert check_version.check(root, None) == []
    lines, code = check_version._format_report(root, None, None)
    assert code == 0
    assert any("skipped" in line for line in lines)


def test_unreleased_only_changelog_fails_with_tag(tmp_path: Path) -> None:
    root = _write_project(tmp_path, changelog_heading="## [Unreleased]\n")
    errors = check_version.check(root, "v0.1.0")
    assert errors
    assert check_version.main(["--root", str(root), "--tag", "v0.1.0"]) == 1


def test_tag_must_match_and_have_date(tmp_path: Path) -> None:
    root = _write_project(
        tmp_path,
        changelog_heading="## [Unreleased]\n\n## [0.1.0] - 2026-10-13\n",
    )
    assert check_version.check(root, "v0.1.0") == []
    root_no_date = _write_project(
        tmp_path / "nodate",
        changelog_heading="## [Unreleased]\n\n## [0.1.0]\n",
    )
    errors = check_version.check(root_no_date, "v0.1.0")
    assert any("date" in e for e in errors)


def test_dist_dir_matching_versions_pass(tmp_path: Path) -> None:
    root = _write_project(
        tmp_path,
        changelog_heading="## [Unreleased]\n\n## [0.1.0] - 2026-10-13\n",
    )
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir)
    _write_sdist(dist_dir)
    assert check_version.check(root, None, dist_dir) == []


def test_dist_dir_version_mismatch_fails(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir, version="0.1.1")
    _write_sdist(dist_dir, version="0.1.1")
    errors = check_version.check(root, None, dist_dir)
    assert errors
    assert check_version.main(["--root", str(root), "--dist-dir", str(dist_dir)]) == 1


def test_wheel_without_schemas_fails(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir, include_required_files=False)
    _write_sdist(dist_dir)
    errors = check_version.check(root, None, dist_dir)
    assert any("missing required" in e for e in errors)


def test_wheel_without_entry_point_fails(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir, include_entry_point=False)
    _write_sdist(dist_dir)
    errors = check_version.check(root, None, dist_dir)
    assert any("entry_points" in e or "console script" in e for e in errors)


def test_sdist_with_internal_dir_fails(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir)
    _write_sdist(dist_dir, extra_members=("internal/tasks/README.md",))
    errors = check_version.check(root, None, dist_dir)
    assert any("internal/" in e for e in errors)


def test_two_wheels_is_usage_error(tmp_path: Path) -> None:
    root = _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir, version="0.1.0")
    _write_wheel(dist_dir, version="0.0.9")
    _write_sdist(dist_dir)
    with pytest.raises(check_version.VersionCheckError):
        check_version.check(root, None, dist_dir)
    assert check_version.main(["--root", str(root), "--dist-dir", str(dist_dir)]) == 2


def test_repository_is_consistent() -> None:
    assert check_version.main(["--root", str(_REPO_ROOT)]) == 0
