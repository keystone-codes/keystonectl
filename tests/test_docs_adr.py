"""Architecture decision record (ADR) documentation checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_ADR_REQUIRED_SECTIONS = (
    "## Status",
    "## Context",
    "## Decision",
    "## Consequences",
    "## Alternatives considered",
    "## Links",
)

_RESERVED_ADR_IDS = frozenset({8, 9})

_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_INTERNAL_TICKET_RE = re.compile(r"\bMD-\d+\b|\bADR-\d{4}\b")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _adr_record_paths() -> list[Path]:
    adr_dir = REPO_ROOT / "docs" / "adr"
    if not adr_dir.is_dir():
        return []
    return sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md"))


def _markdown_outside_adr() -> list[Path]:
    paths: set[Path] = set()
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        paths.add(readme.resolve())
    docs = REPO_ROOT / "docs"
    if docs.is_dir():
        for path in docs.rglob("*.md"):
            rel_parts = path.relative_to(REPO_ROOT).parts
            if rel_parts[:2] == ("docs", "adr"):
                continue
            paths.add(path.resolve())
    return sorted(paths)


def _decision_opening(text: str) -> str | None:
    """First non-empty line under ``## Decision``, or ``None`` if missing."""
    if "## Decision" not in text:
        return None
    after = text.split("## Decision", 1)[1]
    if "## Consequences" in after:
        after = after.split("## Consequences", 1)[0]
    for line in after.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _blank_fences(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        match = _FENCE_RE.match(line)
        if in_fence:
            if match and match.group(1)[0] == fence_char:
                in_fence = False
            out.append("")
            continue
        if match:
            in_fence = True
            fence_char = match.group(1)[0]
            out.append("")
            continue
        out.append(line)
    return out


def _extract_link_targets(line: str) -> list[str]:
    cleaned = _INLINE_CODE_RE.sub("", line)
    return [m.group(1).strip() for m in _MD_LINK_RE.finditer(cleaned)]


def _resolve_relative(from_file: Path, target: str) -> Path:
    path_part = target.split("#", 1)[0]
    if not path_part:
        return from_file
    return (from_file.parent / path_part).resolve()


def _doc_files_for_link_check() -> list[Path]:
    files: list[Path] = []
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        files.append(readme)
    docs = REPO_ROOT / "docs"
    if docs.is_dir():
        files.extend(sorted(docs.rglob("*.md")))
    return files


def test_adr_index_lists_every_record() -> None:
    index_path = REPO_ROOT / "docs" / "adr" / "README.md"
    index_text = index_path.read_text(encoding="utf-8")
    missing = [record.name for record in _adr_record_paths() if record.name not in index_text]
    assert not missing, "docs/adr/README.md does not mention: " + ", ".join(missing)


def test_every_adr_is_referenced_from_docs() -> None:
    outward = _markdown_outside_adr()
    combined = "\n".join(p.read_text(encoding="utf-8") for p in outward)
    unreferenced = [record.name for record in _adr_record_paths() if record.name not in combined]
    assert not unreferenced, "no outward link to ADR file(s): " + ", ".join(unreferenced)


def test_adr_has_required_sections() -> None:
    problems: list[str] = []
    for record in _adr_record_paths():
        text = record.read_text(encoding="utf-8")
        rel = record.relative_to(REPO_ROOT).as_posix()
        for heading in _ADR_REQUIRED_SECTIONS:
            if heading not in text:
                problems.append(f"{rel}: missing {heading}")
        opening = _decision_opening(text)
        if opening is None:
            problems.append(f"{rel}: empty Decision section")
        elif not opening.startswith("We "):
            problems.append(f"{rel}: Decision must start with 'We ', got: {opening[:40]!r}")
    assert not problems, "ADR section gaps:\n" + "\n".join(problems)


def test_adr_numbers_are_unique_and_sequential() -> None:
    numbers: list[int] = []
    for record in _adr_record_paths():
        prefix = record.name.split("-", 1)[0]
        assert len(prefix) == 4 and prefix.isdigit(), f"bad ADR filename: {record.name}"
        numbers.append(int(prefix))
    numbers.sort()
    assert len(numbers) == len(set(numbers)), f"ADR ids repeat: {numbers}"
    # Ids taken by records that are written on other branches and not merged yet.
    # Drop an id from this set once its record lands.
    reserved = _RESERVED_ADR_IDS - set(numbers)
    present = sorted(set(numbers) | reserved)
    assert present == list(range(1, len(present) + 1)), f"ADR ids not sequential from 1: {numbers}"


@pytest.mark.parametrize(
    "md_path", _doc_files_for_link_check(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_relative_links_in_docs_resolve(md_path: Path) -> None:
    raw_lines = md_path.read_text(encoding="utf-8").splitlines()
    link_lines = _blank_fences(raw_lines)
    errors: list[str] = []
    for lineno, line in enumerate(link_lines, start=1):
        for raw in _extract_link_targets(line):
            if raw.startswith(("http://", "https://", "mailto:")):
                continue
            if raw.startswith("#"):
                continue
            path_part = raw.split("#", 1)[0]
            if not path_part:
                continue
            resolved = _resolve_relative(md_path, path_part)
            if not resolved.is_file():
                rel = md_path.relative_to(REPO_ROOT).as_posix()
                errors.append(f"{rel}:{lineno}: {raw} (file not found)")
    assert not errors, "broken relative link(s):\n" + "\n".join(errors)


def test_docs_have_no_internal_ticket_ids() -> None:
    offenders: list[str] = []
    for md_path in _doc_files_for_link_check():
        text = md_path.read_text(encoding="utf-8")
        for match in _INTERNAL_TICKET_RE.finditer(text):
            rel = md_path.relative_to(REPO_ROOT).as_posix()
            offenders.append(f"{rel}: {match.group(0)}")
    assert not offenders, "internal ticket id(s) in docs:\n" + "\n".join(offenders)
