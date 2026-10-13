"""Validation of catalog.yaml and sources.yaml schema_version 1."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from keystonectl.catalog import (
    DEFAULT_CATALOG_REF,
    catalog_errors,
    load_sources,
    sources_errors,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "src" / "keystonectl" / "schemas"
CATALOG_EXAMPLE = ROOT / "docs" / "spec" / "examples" / "catalog.yaml"
SOURCES_EXAMPLE = ROOT / "docs" / "spec" / "examples" / "sources.yaml"
COMMIT = "a" * 40


def _load_example(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_valid_catalog_example() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    assert catalog_errors(document) == []


def test_schemas_are_valid_draft_2020_12() -> None:
    for name in ("catalog-v1.json", "sources-v1.json"):
        schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_package_key_pattern_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["File-MCP"] = document["packages"].pop("file-mcp")
    errors = catalog_errors(document)
    assert any("File-MCP" in error or "packages" in error for error in errors)


@pytest.mark.parametrize("branch", ["main", "master", "HEAD"])
def test_branch_ref_rejected(branch: str) -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["file-mcp"]["versions"][0]["ref"] = branch
    errors = catalog_errors(document)
    assert any(
        f"ref: '{branch}' looks like a branch; use a tag or a commit" in error for error in errors
    )


def test_commit_sha_ref_accepted() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["file-mcp"]["versions"][0]["ref"] = COMMIT
    assert catalog_errors(document) == []


@pytest.mark.parametrize("kind", ["s3", "postgres"])
def test_resource_requires_provides(kind: str) -> None:
    document = _load_example(CATALOG_EXAMPLE)
    entry = copy.deepcopy(document["packages"]["minio"])
    entry["kind"] = "resource"
    del entry["provides"]
    document["packages"][kind] = entry
    errors = catalog_errors(document)
    assert any("provides" in error for error in errors)


def test_service_with_provides_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["file-mcp"]["provides"] = {"kind": "s3", "version": 1}
    errors = catalog_errors(document)
    assert any("provides is not allowed for kind: service" in error for error in errors)


def test_service_version_requires_keystone_mcp() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    del document["packages"]["file-mcp"]["versions"][0]["keystone_mcp"]
    errors = catalog_errors(document)
    assert any("keystone_mcp" in error for error in errors)


def test_resource_version_with_keystone_mcp_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["minio"]["versions"][0]["keystone_mcp"] = ">=0.5.0,<2.0"
    errors = catalog_errors(document)
    assert any("keystone_mcp is not allowed for kind: resource" in error for error in errors)


def test_duplicate_versions_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    duplicate = copy.deepcopy(document["packages"]["file-mcp"]["versions"][0])
    document["packages"]["file-mcp"]["versions"].append(duplicate)
    errors = catalog_errors(document)
    assert any("duplicate version 1.0.0" in error for error in errors)


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://github.com/x/y.git", True),
        ("ssh://git@github.com/x/y.git", True),
        ("git@github.com:x/y.git", True),
        ("file:///tmp/x", True),
        ("http://example.com/x.git", False),
        ("ftp://example.com/x.git", False),
    ],
)
def test_source_url_schemes(url: str, valid: bool) -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["packages"]["file-mcp"]["source"] = url
    errors = catalog_errors(document)
    if valid:
        assert not any("http:// URLs are not allowed" in error for error in errors)
        assert not any("invalid source url" in error for error in errors)
    else:
        assert errors


def test_bundle_unknown_package_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["bundles"]["minimal"]["packages"] = ["missing-pkg"]
    errors = catalog_errors(document)
    assert any("unknown package 'missing-pkg'" in error for error in errors)


def test_bundle_with_resource_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["bundles"]["minimal"]["packages"] = ["minio"]
    errors = catalog_errors(document)
    assert any("'minio' is a resource" in error for error in errors)


def test_bundle_empty_rejected() -> None:
    document = _load_example(CATALOG_EXAMPLE)
    document["bundles"]["minimal"]["packages"] = []
    errors = catalog_errors(document)
    assert any("packages" in error for error in errors)


def test_valid_sources_example() -> None:
    document = _load_example(SOURCES_EXAMPLE)
    assert sources_errors(document) == []


def test_sources_duplicate_name_rejected() -> None:
    document = _load_example(SOURCES_EXAMPLE)
    document["sources"].append(copy.deepcopy(document["sources"][0]))
    errors = sources_errors(document)
    assert any("two sources share name keystone-codes" in error for error in errors)


def test_sources_duplicate_priority_rejected() -> None:
    document = _load_example(SOURCES_EXAMPLE)
    document["sources"][1]["priority"] = 100
    errors = sources_errors(document)
    assert any("two sources share priority 100" in error for error in errors)


def test_default_sources_when_file_absent(tmp_path: Path) -> None:
    sources = load_sources(tmp_path / "missing.yaml")
    assert len(sources) == 1
    assert sources[0].name == "keystone-codes"
    assert sources[0].ref == DEFAULT_CATALOG_REF


def test_sources_sorted_by_priority(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(SOURCES_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    sources = load_sources(path)
    assert [source.name for source in sources] == ["keystone-codes", "partner"]
    assert sources[0].priority == 100
