"""Keep a Changelog line rules for the ``[Unreleased]`` section only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"

_ALLOWED_SUBSECTIONS = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    "Migration",
)

_AREA_PREFIXES = ("e2e:", "docs:", "skill:")

_UNRELEASED_HEADING_RE = re.compile(r"^## \[Unreleased\]\s*$", re.MULTILINE)
_RELEASE_HEADING_RE = re.compile(r"^## \[", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^### (\S+)\s*$", re.MULTILINE)

_FLAG_OUTSIDE_RE = re.compile(r"--[a-z][a-z-]*")
_ERROR_ID_OUTSIDE_RE = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_SNAKE_ID_OUTSIDE_RE = re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b")
_PATH_EXT_OUTSIDE_RE = re.compile(
    r"\b[a-zA-Z0-9_.-]+/[a-zA-Z0-9_./-]+\.(?:md|py|yml|yaml|json|sh|toml)\b"
)


def _extract_unreleased_body(text: str) -> str:
    match = _UNRELEASED_HEADING_RE.search(text)
    if match is None:
        raise AssertionError("CHANGELOG.md has no ## [Unreleased] heading")
    start = match.end()
    rest = text[start:]
    next_heading = _RELEASE_HEADING_RE.search(rest)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end]


def _strip_backticks(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


def _validate_list_line(raw_line: str) -> list[str]:
    errors: list[str] = []
    if not raw_line.startswith("- "):
        return errors
    body = raw_line[2:]
    display = raw_line.strip()

    if len(body) > 160:
        errors.append(f"{display!r}: longer than 160 characters after '- '")

    if not body.endswith("."):
        errors.append(f"{display!r}: must end with a period")

    start = body
    for prefix in _AREA_PREFIXES:
        if body.startswith(prefix):
            start = body[len(prefix) :].lstrip()
            break

    if start.startswith("`"):
        pass
    elif not start or not start[0].isupper():
        errors.append(
            f"{display!r}: must start with a capital letter, a backtick identifier, "
            f"or an area prefix ({', '.join(_AREA_PREFIXES)})"
        )

    outside = _strip_backticks(body)
    if _FLAG_OUTSIDE_RE.search(outside):
        errors.append(f"{display!r}: CLI flags must appear in backticks")
    if _ERROR_ID_OUTSIDE_RE.search(outside):
        errors.append(f"{display!r}: error or warning ids must appear in backticks")
    if _SNAKE_ID_OUTSIDE_RE.search(outside):
        errors.append(f"{display!r}: snake_case identifiers must appear in backticks")
    if _PATH_EXT_OUTSIDE_RE.search(outside):
        errors.append(f"{display!r}: file paths must appear in backticks")

    return errors


def validate_unreleased_changelog(text: str) -> list[str]:
    """Return human-readable violations for ``[Unreleased]`` (empty list if OK)."""
    body = _extract_unreleased_body(text)
    errors: list[str] = []

    seen_subsections: list[str] = []
    current_subsection: str | None = None

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            name = stripped.removeprefix("### ").strip()
            if name not in _ALLOWED_SUBSECTIONS:
                errors.append(
                    f"subsection ### {name}: unknown subsection "
                    f"(allowed: {', '.join(_ALLOWED_SUBSECTIONS)})"
                )
            seen_subsections.append(name)
            current_subsection = name
            continue
        if stripped.startswith("- "):
            if current_subsection is None:
                errors.append(f"{stripped!r}: list item outside a ### subsection")
            errors.extend(_validate_list_line(stripped))
            continue
        errors.append(f"{stripped!r}: unexpected content in [Unreleased]")

    allowed_index = {name: idx for idx, name in enumerate(_ALLOWED_SUBSECTIONS)}
    last_index = -1
    for name in seen_subsections:
        if name not in allowed_index:
            continue
        idx = allowed_index[name]
        if idx < last_index:
            errors.append(
                f"subsection order: ### {name} must follow {', '.join(_ALLOWED_SUBSECTIONS)}"
            )
        last_index = max(last_index, idx)

    return errors


def _assert_unreleased_ok(text: str) -> None:
    errors = validate_unreleased_changelog(text)
    assert not errors, "CHANGELOG [Unreleased] style violations:\n" + "\n".join(errors)


def test_changelog_unreleased_matches_style_rules() -> None:
    text = _CHANGELOG_PATH.read_text(encoding="utf-8")
    _assert_unreleased_ok(text)


def test_empty_unreleased_section_passes() -> None:
    sample = """# Changelog

## [Unreleased]

## [0.1.1] - 2026-10-15

### Added

- Added something fine.
"""
    _assert_unreleased_ok(sample)


@pytest.mark.parametrize(
    ("changelog", "fragment"),
    [
        (
            """## [Unreleased]

### Changed

- Missing terminal punctuation
""",
            "must end with a period",
        ),
        (
            """## [Unreleased]

### Changed

"""
            + "- "
            + ("x" * 161)
            + "\n",
            "longer than 160 characters",
        ),
        (
            """## [Unreleased]

### Changed

- Setup fails with KEYSTONE_SETUP_X without quoting.
""",
            "error or warning ids must appear in backticks",
        ),
        (
            """## [Unreleased]

### Notes

- A note.
""",
            "unknown subsection",
        ),
        (
            """## [Unreleased]

### Changed

- Changed something.

### Added

- Added something.
""",
            "subsection order",
        ),
    ],
)
def test_unreleased_style_rejects_invalid_lines(changelog: str, fragment: str) -> None:
    errors = validate_unreleased_changelog("# Changelog\n\n" + changelog)
    joined = "\n".join(errors)
    assert fragment in joined, joined
