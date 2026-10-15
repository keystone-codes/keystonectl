"""Static checks on GitHub Actions workflow files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOW_FILES = sorted(WORKFLOWS_DIR.glob("*.yml"))

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_COMMENT_RE = re.compile(r"^v\d")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _workflow_on(doc: dict) -> dict:
    # PyYAML 1.1 treats the key `on` as boolean True.
    return doc.get("on") or doc[True]


def _iter_uses(doc: dict):
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if uses:
                yield uses


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=[p.name for p in WORKFLOW_FILES])
def test_workflow_file_parses_as_yaml(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert "jobs" in doc


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=[p.name for p in WORKFLOW_FILES])
def test_every_third_party_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for uses in _iter_uses(doc):
        if uses.startswith("./"):
            continue
        action, _, ref = uses.partition("@")
        assert ref, f"{path.name}: '{uses}' has no @ref"
        ref_part, _, comment = ref.partition(" ")
        assert _COMMIT_SHA_RE.match(ref_part), (
            f"{path.name}: '{action}' is pinned to '{ref_part}', not a 40-character commit SHA"
        )
        if comment.startswith("#"):
            version = comment.removeprefix("#").strip()
            assert _VERSION_COMMENT_RE.match(version), (
                f"{path.name}: '{action}' comment should be a version like '# v7', got {comment!r}"
            )


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_declares_a_restrictive_top_level_permissions_block(name: str) -> None:
    doc = _load(name)
    assert doc.get("permissions") == {"contents": "read"}, (
        f"{name} must declare 'permissions: contents: read' at the top level"
    )


def test_release_job_permissions_are_not_widened_by_the_top_level_block() -> None:
    doc = _load("release.yml")
    jobs = doc["jobs"]
    assert jobs["pypi"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert "permissions" not in jobs["wheel-smoke"]
    assert "permissions" not in jobs["version-gate"]
    assert "permissions" not in jobs["build-dist"]


def test_pypi_job_uses_environment_pypi() -> None:
    doc = _load("release.yml")
    assert doc["jobs"]["pypi"]["environment"] == "pypi"


def test_release_triggers_only_on_version_tags_and_dispatch() -> None:
    doc = _load("release.yml")
    on = _workflow_on(doc)
    assert on["push"]["tags"] == ["v*.*.*"]
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


def _needs(job: dict) -> list[str]:
    needs = job.get("needs")
    if needs is None:
        return []
    return [needs] if isinstance(needs, str) else list(needs)


def test_pypi_needs_test_and_wheel_smoke() -> None:
    doc = _load("release.yml")
    needs = _needs(doc["jobs"]["pypi"])
    assert "test" in needs
    assert "wheel-smoke" in needs


def test_github_release_needs_pypi() -> None:
    doc = _load("release.yml")
    needs = _needs(doc["jobs"]["github-release"])
    assert "pypi" in needs
    assert "pypi-verify" in needs


def test_version_gate_checks_the_default_catalog_ref_exists() -> None:
    doc = _load("release.yml")
    steps = doc["jobs"]["version-gate"]["steps"]
    ls_remote_steps = [s for s in steps if "ls-remote" in (s.get("run") or "")]
    assert ls_remote_steps, "version-gate must have a step that runs 'git ls-remote'"
    run = ls_remote_steps[0]["run"]
    assert "DEFAULT_SOURCES" in run
    assert "--tags" in run


def test_e2e_paths_include_the_script_and_its_unit_tests() -> None:
    doc = _load("e2e.yml")
    paths = _workflow_on(doc)["pull_request"]["paths"]
    assert paths == [
        "src/**",
        "schemas/**",
        "nginx/**",
        "scripts/e2e*/**",
        "scripts/e2e.sh",
        "tests/e2e/**",
        "tests/test_e2e_*.py",
        "skills/**",
        ".github/workflows/e2e.yml",
    ]


def test_e2e_report_is_uploaded_even_when_the_run_passes() -> None:
    doc = _load("e2e.yml")
    steps = doc["jobs"]["e2e"]["steps"]
    upload_steps = [s for s in steps if (s.get("uses") or "").startswith("actions/upload-artifact")]
    assert upload_steps, "e2e job must upload the report"
    assert upload_steps[0].get("if") == "always()"


def test_no_pypi_token_secret_is_referenced() -> None:
    text = (WORKFLOWS_DIR / "release.yml").read_text(encoding="utf-8")
    assert "secrets.PYPI" not in text
    assert "password:" not in text


def test_e2e_dispatch_accepts_partner_catalog_url() -> None:
    doc = _load("e2e.yml")
    on = _workflow_on(doc)
    partner = on["workflow_dispatch"]["inputs"]["partner_catalog_url"]
    assert partner.get("required") is False
    assert partner.get("default", "") == ""
    assert (
        doc["jobs"]["e2e"]["env"]["E2E_PARTNER_CATALOG_URL"] == "${{ inputs.partner_catalog_url }}"
    )
    assert on["pull_request"]["paths"] == [
        "src/**",
        "schemas/**",
        "nginx/**",
        "scripts/e2e*/**",
        "scripts/e2e.sh",
        "tests/e2e/**",
        "tests/test_e2e_*.py",
        "skills/**",
        ".github/workflows/e2e.yml",
    ]
    assert doc["jobs"]["e2e"]["strategy"]["matrix"]["layout"] == [
        "production-example",
        "keystone-deploy",
    ]
    runs = [step.get("run") or "" for step in doc["jobs"]["e2e"]["steps"]]
    assert any("--profile ci" in run for run in runs)
    assert not any("--profile vm" in run for run in runs)
