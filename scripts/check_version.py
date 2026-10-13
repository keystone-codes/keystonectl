#!/usr/bin/env python3
"""Check that the project version agrees across its sources of truth.

Sources checked:

- ``pyproject.toml`` -> ``project.version``
- ``src/keystonectl/__init__.py`` -> ``__version__`` (read as text)
- ``CHANGELOG.md`` -> the first released heading (skipped without ``--tag`` when
  only ``[Unreleased]`` is present — K43)

With ``--tag``, additionally checks that the tag starts with ``v`` and that
``tag[1:]`` matches all checked sources, and that the matching CHANGELOG
heading carries a release date.

With ``--dist-dir``, additionally checks the wheel and sdist built by
``uv build`` into that directory.

Exit codes: 0 everything agrees, 1 a mismatch was found, 2 a source file is
missing or unparsable, or ``--dist-dir`` does not contain exactly one wheel
and one sdist of this project's distribution.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DUNDER_VERSION_RE = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']\s*$", re.MULTILINE)
_CHANGELOG_HEADING_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)

PYPROJECT_SOURCE = "pyproject.toml"
DUNDER_SOURCE = "src/keystonectl/__init__.py"
CHANGELOG_SOURCE = "CHANGELOG.md"

DIST_NAME = "keystonectl"
NORMALIZED_DIST_NAME = "keystonectl"

_WHEEL_RE = re.compile(rf"^{re.escape(NORMALIZED_DIST_NAME)}-(?P<version>[^-]+)-py3-none-any\.whl$")
_SDIST_RE = re.compile(rf"^{re.escape(NORMALIZED_DIST_NAME)}-(?P<version>.+)\.tar\.gz$")

REQUIRED_WHEEL_FILES: tuple[str, ...] = (
    "keystonectl/py.typed",
    "keystonectl/schemas/service-package-v1.json",
    "keystonectl/schemas/catalog-v1.json",
    "keystonectl/schemas/sources-v1.json",
    "keystonectl/schemas/lock-v1.json",
    "keystonectl/schemas/output.schema.json",
)

_CONSOLE_SCRIPT_LINE = "keystonectl = keystonectl.cli:main"

FORBIDDEN_SDIST_PREFIXES: tuple[str, ...] = (
    "internal/",
    "tests/",
    ".env",
    ".keystonectl/",
    "dist/",
)


class VersionCheckError(Exception):
    """Raised when a source file is missing or cannot be parsed."""


def _read_pyproject_version(root: Path) -> str:
    path = root / PYPROJECT_SOURCE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VersionCheckError(f"{PYPROJECT_SOURCE}: file not found ({path})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise VersionCheckError(f"{PYPROJECT_SOURCE}: could not parse TOML ({exc})") from exc

    try:
        version = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise VersionCheckError(f"{PYPROJECT_SOURCE}: missing [project].version") from exc

    if not isinstance(version, str):
        raise VersionCheckError(f"{PYPROJECT_SOURCE}: [project].version is not a string")
    return version


def _read_dunder_version(root: Path) -> str:
    path = root / DUNDER_SOURCE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VersionCheckError(f"{DUNDER_SOURCE}: file not found ({path})") from exc

    match = _DUNDER_VERSION_RE.search(text)
    if match is None:
        raise VersionCheckError(f"{DUNDER_SOURCE}: no __version__ assignment found")
    return match.group(1)


def _read_changelog_text(root: Path) -> str:
    path = root / CHANGELOG_SOURCE
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VersionCheckError(f"{CHANGELOG_SOURCE}: file not found ({path})") from exc


def _first_released_changelog(text: str) -> tuple[str, str | None] | None:
    for match in _CHANGELOG_HEADING_RE.finditer(text):
        version = match.group("version")
        if version.strip().lower() == "unreleased":
            continue
        return version, match.group("date")
    return None


def read_versions(root: Path, *, include_changelog: bool) -> dict[str, str]:
    """Read version strings from each source under ``root``."""
    pyproject_version = _read_pyproject_version(root)
    dunder_version = _read_dunder_version(root)
    versions = {
        PYPROJECT_SOURCE: pyproject_version,
        DUNDER_SOURCE: dunder_version,
    }
    if include_changelog:
        release = _first_released_changelog(_read_changelog_text(root))
        if release is None:
            raise VersionCheckError(
                f"{CHANGELOG_SOURCE}: no released heading found (only [Unreleased]?)"
            )
        changelog_version, _date = release
        versions[CHANGELOG_SOURCE] = changelog_version
    return versions


def _find_dist_files(dist_dir: Path, *, pattern: re.Pattern[str], kind: str) -> Path:
    if not dist_dir.is_dir():
        raise VersionCheckError(f"--dist-dir {dist_dir}: not a directory")

    matches = sorted(p for p in dist_dir.iterdir() if pattern.match(p.name))
    if len(matches) != 1:
        found = ", ".join(p.name for p in matches) or "(none)"
        raise VersionCheckError(
            f"--dist-dir {dist_dir}: expected exactly one {NORMALIZED_DIST_NAME} {kind}, "
            f"found {len(matches)}: {found}"
        )
    return matches[0]


def _check_wheel(wheel_path: Path, pyproject_version: str) -> list[str]:
    errors: list[str] = []

    match = _WHEEL_RE.match(wheel_path.name)
    assert match is not None
    wheel_version = match.group("version")
    if wheel_version != pyproject_version:
        errors.append(
            f"{wheel_path.name}: wheel filename version ({wheel_version}) != "
            f"{PYPROJECT_SOURCE} ({pyproject_version})"
        )

    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = frozenset(archive.namelist())
            metadata_name = next(
                (name for name in names if name.endswith(".dist-info/METADATA")), None
            )
            if metadata_name is None:
                errors.append(f"{wheel_path.name}: no *.dist-info/METADATA found")
            else:
                metadata_lines = archive.read(metadata_name).decode("utf-8").splitlines()
                if f"Version: {pyproject_version}" not in metadata_lines:
                    errors.append(
                        f"{wheel_path.name}: {metadata_name} does not declare "
                        f"'Version: {pyproject_version}'"
                    )
                if f"Name: {DIST_NAME}" not in metadata_lines:
                    errors.append(
                        f"{wheel_path.name}: {metadata_name} does not declare 'Name: {DIST_NAME}'"
                    )
                if "License-Expression: Apache-2.0" not in metadata_lines:
                    errors.append(
                        f"{wheel_path.name}: {metadata_name} does not declare "
                        "'License-Expression: Apache-2.0'"
                    )

            missing_files = [name for name in REQUIRED_WHEEL_FILES if name not in names]
            if missing_files:
                errors.append(
                    f"{wheel_path.name}: missing required file(s): {', '.join(missing_files)}"
                )

            entry_points_name = next(
                (name for name in names if name.endswith(".dist-info/entry_points.txt")), None
            )
            if entry_points_name is None:
                errors.append(f"{wheel_path.name}: no *.dist-info/entry_points.txt found")
            else:
                body = archive.read(entry_points_name).decode("utf-8")
                if _CONSOLE_SCRIPT_LINE not in body:
                    errors.append(
                        f"{wheel_path.name}: {entry_points_name} missing console script "
                        f"{_CONSOLE_SCRIPT_LINE!r}"
                    )
    except zipfile.BadZipFile as exc:
        raise VersionCheckError(f"{wheel_path}: not a valid wheel (zip) file: {exc}") from exc

    return errors


def _check_sdist(sdist_path: Path, pyproject_version: str) -> list[str]:
    errors: list[str] = []

    match = _SDIST_RE.match(sdist_path.name)
    assert match is not None
    sdist_version = match.group("version")
    if sdist_version != pyproject_version:
        errors.append(
            f"{sdist_path.name}: sdist filename version ({sdist_version}) != "
            f"{PYPROJECT_SOURCE} ({pyproject_version})"
        )

    try:
        with tarfile.open(sdist_path, mode="r:gz") as archive:
            member_names = archive.getnames()
    except tarfile.TarError as exc:
        raise VersionCheckError(f"{sdist_path}: not a valid sdist (tar.gz) file: {exc}") from exc

    forbidden: list[str] = []
    for name in member_names:
        _root_dir, _, relative = name.partition("/")
        if not relative:
            continue
        if relative.startswith(FORBIDDEN_SDIST_PREFIXES):
            forbidden.append(name)
    if forbidden:
        errors.append(
            f"{sdist_path.name}: contains forbidden path(s): {', '.join(sorted(forbidden))}"
        )

    return errors


def check_dist_dir(root: Path, dist_dir: Path) -> list[str]:
    pyproject_version = _read_pyproject_version(root)
    wheel_path = _find_dist_files(dist_dir, pattern=_WHEEL_RE, kind="wheel")
    sdist_path = _find_dist_files(dist_dir, pattern=_SDIST_RE, kind="sdist")
    return [
        *_check_wheel(wheel_path, pyproject_version),
        *_check_sdist(sdist_path, pyproject_version),
    ]


def check(root: Path, tag: str | None, dist_dir: Path | None = None) -> list[str]:
    changelog_text = _read_changelog_text(root)
    changelog_release = _first_released_changelog(changelog_text)
    changelog_skipped = tag is None and changelog_release is None

    if tag is not None and changelog_release is None:
        return [
            (
                f"{CHANGELOG_SOURCE}: no released heading for release tag {tag!r} "
                "(expected ## [X.Y.Z] - YYYY-MM-DD)"
            )
        ]

    versions = read_versions(root, include_changelog=not changelog_skipped)
    changelog_date = changelog_release[1] if changelog_release else None

    errors: list[str] = []

    for source, version in versions.items():
        if not _VERSION_RE.match(version):
            errors.append(
                f"{source}: version {version!r} is not a plain X.Y.Z release "
                "version (pre-release versions are not supported)"
            )

    pyproject_version = versions[PYPROJECT_SOURCE]
    dunder_version = versions[DUNDER_SOURCE]

    if pyproject_version != dunder_version:
        errors.append(
            f"{PYPROJECT_SOURCE} ({pyproject_version}) != {DUNDER_SOURCE} ({dunder_version})"
        )

    if not changelog_skipped:
        changelog_version = versions[CHANGELOG_SOURCE]
        if pyproject_version != changelog_version:
            errors.append(
                f"{PYPROJECT_SOURCE} ({pyproject_version}) != "
                f"{CHANGELOG_SOURCE} ({changelog_version})"
            )
        if dunder_version != changelog_version:
            errors.append(
                f"{DUNDER_SOURCE} ({dunder_version}) != {CHANGELOG_SOURCE} ({changelog_version})"
            )

    if tag is not None:
        if not tag.startswith("v"):
            errors.append(f"tag {tag!r} does not start with 'v'")
        else:
            tag_version = tag[1:]
            if not _VERSION_RE.match(tag_version):
                errors.append(
                    f"tag {tag!r} is not a plain vX.Y.Z release tag "
                    "(pre-release tags are not supported)"
                )
            else:
                for source, version in versions.items():
                    if version != tag_version:
                        errors.append(f"tag ({tag_version}) != {source} ({version})")
                if (
                    changelog_release
                    and tag_version == changelog_release[0]
                    and changelog_date is None
                ):
                    errors.append(
                        f"{CHANGELOG_SOURCE}: heading for {tag_version} has no release date"
                    )

    if dist_dir is not None:
        errors.extend(check_dist_dir(root, dist_dir))

    return errors


def _format_report(
    root: Path, tag: str | None, dist_dir: Path | None = None
) -> tuple[list[str], int]:
    lines: list[str] = []

    try:
        pyproject_version = _read_pyproject_version(root)
        dunder_version = _read_dunder_version(root)
        changelog_text = _read_changelog_text(root)
        changelog_release = _first_released_changelog(changelog_text)
    except VersionCheckError as exc:
        return [str(exc)], 2

    changelog_skipped = tag is None and changelog_release is None

    lines.append(f"{PYPROJECT_SOURCE}: {pyproject_version}")
    lines.append(f"{DUNDER_SOURCE}: {dunder_version}")
    if changelog_skipped:
        lines.append(f"{CHANGELOG_SOURCE}: no released version yet; skipped")
    elif changelog_release is not None:
        changelog_version, changelog_date = changelog_release
        if changelog_date:
            lines.append(f"{CHANGELOG_SOURCE}: {changelog_version} ({changelog_date})")
        else:
            lines.append(f"{CHANGELOG_SOURCE}: {changelog_version}")
    if tag is not None:
        lines.append(f"tag: {tag}")
    if dist_dir is not None:
        lines.append(f"dist-dir: {dist_dir}")

    try:
        errors = check(root, tag, dist_dir)
    except VersionCheckError as exc:
        lines.append(str(exc))
        return lines, 2

    if errors:
        lines.append("MISMATCH: " + "; ".join(errors))
        return lines, 1

    lines.append("OK")
    return lines, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default=None,
        help="Git tag to check against (e.g. v0.1.0); omit to only check file sources",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root (default: parent of this script's directory)",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Directory holding wheel and sdist from 'uv build' (e.g. dist/)",
    )
    args = parser.parse_args(argv)

    lines, exit_code = _format_report(args.root, args.tag, args.dist_dir)
    for line in lines:
        print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
