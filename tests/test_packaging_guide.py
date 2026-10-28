"""Checks for docs/packaging-a-service.md (E02)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from keystonectl.lint import lint_manifest
from keystonectl.manifest import manifest_errors

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "packaging-a-service.md"

# The forbidden list stores SHA-256 hashes of lower-cased words/domains, not
# the words themselves, so the public repository never carries the plain
# text of an internal name again (see docs/adr and the Т2б scrub task).
FORBIDDEN_HASHES = Path(__file__).resolve().parent / "support" / "forbidden_words.sha256"

_FORBIDDEN_WORD_RE = re.compile(r"[a-z0-9]+")
_FORBIDDEN_DOMAIN_RE = re.compile(r"[a-z0-9.-]+")

# Markdown files that may legitimately carry a name the scan would otherwise
# flag. Keep this empty unless a real run below actually fails on a file;
# list the file and the reason here instead of weakening the scan itself.
_FORBIDDEN_SCAN_EXCEPTIONS: dict[Path, str] = {}


def _forbidden_hashes() -> set[str]:
    assert FORBIDDEN_HASHES.is_file()
    return {
        line.strip()
        for line in FORBIDDEN_HASHES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _token_hashes(text: str) -> set[str]:
    """Split text into candidate words/domains and hash each one.

    Two tokenizers run over the same lower-cased text: ``[a-z0-9]+`` catches
    a bare internal word (e.g. a codename), ``[a-z0-9.-]+`` additionally
    keeps dots and hyphens so a whole forbidden domain (e.g.
    ``sub.example.com``) is hashed as one token instead of being split into
    harmless fragments.
    """
    lowered = text.lower()
    tokens = set(_FORBIDDEN_WORD_RE.findall(lowered))
    tokens |= set(_FORBIDDEN_DOMAIN_RE.findall(lowered))
    return {hashlib.sha256(token.encode("utf-8")).hexdigest() for token in tokens}


def _markdown_scan_targets() -> list[Path]:
    files = {GUIDE}
    for root_name in ("docs", "skills"):
        root = REPO_ROOT / root_name
        if root.is_dir():
            files.update(root.rglob("*.md"))
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        files.add(readme)
    return sorted(f for f in files if f not in _FORBIDDEN_SCAN_EXCEPTIONS)


_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_EXAMPLE_MARKER_RE = re.compile(r"<!--\s*example:\s*([^>]+)\s*-->")
_IMAGE_LINE_RE = re.compile(
    r"^\s*image:\s*(.+)$",
    re.MULTILINE,
)
_DIGEST_IMAGE_RE = re.compile(r"@[sS][hH][aA]256:[0-9a-fA-F]{64}\s*(?:#.*)?$")
_GITHUB_KEystone_RE = re.compile(r"https://github\.com/keystone-codes/[^)\s]+")
_PINNED_REF_RE = re.compile(r"/(?:blob|tree)/v[0-9]+\.[0-9]+\.[0-9]+(?:/|$|[#?])")


def _github_anchor(title: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", title.strip())
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text.strip())
    return text


def _guide_text() -> str:
    assert GUIDE.is_file(), "docs/packaging-a-service.md is missing"
    return GUIDE.read_text(encoding="utf-8")


def _headings(text: str) -> dict[str, str]:
    anchors: dict[str, str] = {}
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        anchor = _github_anchor(title)
        anchors[anchor] = title
    return anchors


def _iter_fenced_blocks(text: str) -> list[tuple[str | None, str, int]]:
    """Return (example_kind, body, start_line) for each fenced block."""
    lines = text.splitlines()
    blocks: list[tuple[str | None, str, int]] = []
    in_fence = False
    fence_char = ""
    kind: str | None = None
    body: list[str] = []
    start = 0
    for index, line in enumerate(lines, start=1):
        if not in_fence:
            marker = _EXAMPLE_MARKER_RE.search(line)
            if marker:
                kind = marker.group(1).strip()
            fence = _FENCE_RE.match(line)
            if fence:
                in_fence = True
                fence_char = fence.group(1)[0]
                body = []
                start = index + 1
            continue
        fence = _FENCE_RE.match(line)
        if fence and fence.group(1)[0] == fence_char:
            blocks.append((kind, "\n".join(body), start))
            in_fence = False
            kind = None
            body = []
            continue
        body.append(line)
    return blocks


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
    cleaned = re.sub(r"`[^`\n]*`", "", line)
    return [m.group(1).strip() for m in _MD_LINK_RE.finditer(cleaned)]


def test_relative_links_resolve() -> None:
    text = _guide_text()
    lines = _blank_fences(text.splitlines())
    headings = _headings(text)
    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        for raw in _extract_link_targets(line):
            if raw.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, fragment = raw.partition("#")
            if raw.startswith("#"):
                anchor = raw[1:]
                if anchor and anchor not in headings:
                    errors.append(f"{lineno}: internal anchor #{anchor} missing")
                continue
            if not path_part:
                continue
            resolved = (GUIDE.parent / path_part).resolve()
            if not resolved.is_file():
                errors.append(f"{lineno}: {raw} (file not found)")
                continue
            if fragment and resolved == GUIDE.resolve():
                if fragment not in headings:
                    errors.append(f"{lineno}: anchor #{fragment} missing in guide")
            elif fragment and resolved.suffix == ".md":
                other_headings = _headings(resolved.read_text(encoding="utf-8"))
                if fragment not in other_headings:
                    errors.append(f"{lineno}: anchor #{fragment} missing in {path_part}")
    assert not errors, "broken link(s):\n" + "\n".join(errors)


def test_internal_anchors_resolve() -> None:
    text = _guide_text()
    headings = _headings(text)
    errors: list[str] = []
    for match in _MD_LINK_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw.startswith("#"):
            continue
        anchor = raw[1:]
        if anchor and anchor not in headings:
            errors.append(f"#{anchor}")
    assert not errors, "unknown internal anchor(s): " + ", ".join(errors)


def test_external_keystone_links_are_pinned() -> None:
    text = _guide_text()
    link_lines = _blank_fences(text.splitlines())
    errors: list[str] = []
    for line in link_lines:
        for raw in _extract_link_targets(line):
            if not raw.startswith("https://github.com/keystone-codes/"):
                continue
            url = raw.split("#", 1)[0]
            if "/blob/main/" in url or "/tree/main/" in url:
                errors.append(url)
            if "/blob/master/" in url or "/tree/master/" in url:
                errors.append(url)
            if not _PINNED_REF_RE.search(url):
                errors.append(url)
    assert not errors, "unpinned keystone-codes link(s):\n" + "\n".join(errors)


def test_example_manifests_are_valid(tmp_path: Path) -> None:
    text = _guide_text()
    blocks = _iter_fenced_blocks(text)
    marked = [
        (kind, body)
        for kind, body, _start in blocks
        if kind == "keystone-service.yaml" and body.strip()
    ]
    assert len(marked) >= 3, "expected at least three keystone-service.yaml examples"
    for index, (_kind, body) in enumerate(marked):
        document = yaml.safe_load(body)
        assert isinstance(document, dict)
        schema_errors = manifest_errors(document)
        assert schema_errors == [], schema_errors
        directory = tmp_path / f"pkg-{index}"
        directory.mkdir()
        (directory / "keystone-service.yaml").write_text(body, encoding="utf-8")
        policy = document.get("recommended_policy")
        if isinstance(policy, str):
            (directory / policy).write_text('version: "2.0.0"\nroles: {}\n', encoding="utf-8")
        problems, _warnings = lint_manifest(
            directory / "keystone-service.yaml",
            directory,
            keystone_version=None,
            check_files=True,
        )
        errors = [item for item in problems if item["severity"] == "error"]
        assert not errors, errors


def test_example_images_are_pinned() -> None:
    text = _guide_text()
    blocks = _iter_fenced_blocks(text)
    for kind, body, start in blocks:
        if not body.strip():
            continue
        if kind and kind.endswith("fragment"):
            continue
        if not re.search(r"^\s*image:\s*", body, re.MULTILINE):
            continue
        for match in _IMAGE_LINE_RE.finditer(body):
            value = match.group(1).strip().strip('"').strip("'")
            if not _DIGEST_IMAGE_RE.search(value):
                pytest.fail(f"line ~{start}: image not digest-pinned: {value!r}")


def test_no_v0_run_section() -> None:
    text = _guide_text()
    for kind, body, _start in _iter_fenced_blocks(text):
        if kind != "keystone-service.yaml":
            continue
        document = yaml.safe_load(body)
        assert isinstance(document, dict)
        assert "run" not in document, "top-level run: is v0; use containers"


def test_checklist_table_present() -> None:
    text = _guide_text()
    checklist = text.split("## Checklist", 1)
    assert len(checklist) == 2, "missing ## Checklist"
    section = checklist[1]
    assert "|" in section and "---" in section
    lower = section.lower()
    for needle in (
        "bearer",
        "x-keystone-identity",
        "digest",
        "containers",
        "keystonectl lint",
    ):
        assert needle in lower, f"checklist missing {needle!r}"


def test_no_customer_names() -> None:
    forbidden = _forbidden_hashes()
    offenders: list[tuple[str, list[str]]] = []
    for path in _markdown_scan_targets():
        text = path.read_text(encoding="utf-8")
        hit_hashes = sorted(_token_hashes(text) & forbidden)
        if hit_hashes:
            offenders.append((path.relative_to(REPO_ROOT).as_posix(), hit_hashes))
    # Only the hash and the file are reported: a hit here would otherwise
    # print the very name this test exists to keep out of the repository.
    assert not offenders, f"forbidden word hash(es) found: {offenders}"


@pytest.mark.parametrize(
    ("text", "should_match"),
    [
        pytest.param(
            "Some guide text that names the `zzz-scrub-test-codename` stand by mistake.",
            True,
            id="leak",
        ),
        pytest.param(
            "Some guide text with no internal names in it at all.",
            False,
            id="clean",
        ),
    ],
)
def test_forbidden_word_scan_catches_a_synthetic_leak(text: str, should_match: bool) -> None:
    """Prove the tokenize-and-hash mechanism actually detects a leak.

    ``zzz-scrub-test-codename`` is a synthetic placeholder invented for this
    test only (it is not a real internal name), so the real forbidden words
    never have to appear in this file to prove the scan works.
    """
    synthetic_hash = hashlib.sha256(b"zzz-scrub-test-codename").hexdigest()
    hit = synthetic_hash in _token_hashes(text)
    assert hit is should_match
