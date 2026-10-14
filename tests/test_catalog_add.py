"""catalog add: schema, conflicts, priority, and reachability."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from keystonectl.catalog import DEFAULT_CATALOG_REF
from keystonectl.sources import add_source
from tests.support.served_catalog import SHA, catalog_runner

PARTNER = "https://example.com/partner.git"
CODES = "https://example.com/keystone-codes.git"
BROKEN = "https://example.com/broken.git"

_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_default_catalog_ref_is_a_tag_or_commit() -> None:
    assert _TAG_RE.match(DEFAULT_CATALOG_REF) or _SHA_RE.match(DEFAULT_CATALOG_REF), (
        f"DEFAULT_CATALOG_REF {DEFAULT_CATALOG_REF!r} must be a vX.Y.Z tag or a 40-char commit SHA"
    )
    assert DEFAULT_CATALOG_REF not in {"main", "master"}


def _sources(root: Path, *, priority: int = 100) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "sources": [
            {"name": "keystone-codes", "url": CODES, "ref": "v1.0.0", "priority": priority}
        ],
    }
    (root / "sources.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    cache = root / ".keystonectl" / "catalogs" / "keystone-codes"
    cache.mkdir(parents=True)
    catalog = Path("tests/data/catalogs/keystone-codes/catalog.yaml").read_text(encoding="utf-8")
    (cache / "catalog.yaml").write_text(catalog, encoding="utf-8")


def test_catalog_add_ok_writes_sources_with_backup(tmp_path: Path) -> None:
    _sources(tmp_path)
    result = add_source(tmp_path, PARTNER, catalog_runner(), ref="v1.0.0")
    assert result.code == 0, result.error
    assert (tmp_path / "sources.yaml.bak").is_file()
    document = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    names = {item["name"] for item in document["sources"]}
    assert names == {"keystone-codes", "partner"}
    assert result.result["packages"] == 1
    assert result.result["bundles"] == ["minimal"]


def test_catalog_add_ref_chosen_from_tags(tmp_path: Path) -> None:
    _sources(tmp_path)
    result = add_source(tmp_path, PARTNER, catalog_runner())
    assert result.code == 0, result.error
    assert result.result["ref"] == "v1.2.0"
    assert any(
        item["id"] == "catalog_ref_chosen" and "v1.2.0" in item["message"]
        for item in result.warnings
    )


def test_catalog_add_no_tags(tmp_path: Path) -> None:
    result = add_source(tmp_path, PARTNER, catalog_runner(tags=""))
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "tag_not_found"
    assert not (tmp_path / "sources.yaml").exists()


def test_catalog_add_http_refused(tmp_path: Path) -> None:
    result = add_source(tmp_path, "http://example.com/partner.git", catalog_runner())
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "source_invalid"


def test_catalog_add_schema_invalid_leaves_sources_untouched(tmp_path: Path) -> None:
    _sources(tmp_path)
    before = (tmp_path / "sources.yaml").read_bytes()
    result = add_source(
        tmp_path, BROKEN, catalog_runner(), name="broken", ref="v1.0.0", priority=50
    )
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "source_invalid"
    assert result.result["problems"]
    assert (tmp_path / "sources.yaml").read_bytes() == before
    assert not (tmp_path / "sources.yaml.bak").exists()


def test_catalog_add_source_exists_by_url_and_by_name(tmp_path: Path) -> None:
    _sources(tmp_path)
    by_url = add_source(tmp_path, CODES, catalog_runner(), name="other", ref="v1.0.0", priority=40)
    assert by_url.code == 3
    assert by_url.error is not None
    assert by_url.error["id"] == "source_exists"
    by_name = add_source(
        tmp_path, PARTNER, catalog_runner(), name="keystone-codes", ref="v1.0.0", priority=40
    )
    assert by_name.code == 3
    assert by_name.error is not None
    assert by_name.error["id"] == "source_exists"
    assert "use --name" in by_name.error["message"]


def test_catalog_add_priority_default_and_taken(tmp_path: Path) -> None:
    _sources(tmp_path, priority=100)
    chosen = add_source(tmp_path, PARTNER, catalog_runner(), ref="v1.0.0")
    assert chosen.code == 0, chosen.error
    assert chosen.result["priority"] == 90
    taken = add_source(
        tmp_path,
        "https://example.com/other.git",
        catalog_runner(),
        name="again",
        ref="v1.0.0",
        priority=100,
    )
    assert taken.code == 3
    assert taken.error is not None
    assert taken.error["id"] == "priority_taken"


def test_catalog_add_name_conflicts_warn_and_listed(tmp_path: Path) -> None:
    _sources(tmp_path)
    result = add_source(tmp_path, PARTNER, catalog_runner(), ref="v1.0.0", priority=90)
    assert result.code == 0, result.error
    ids = {item["id"] for item in result.warnings}
    assert "package_name_shadowed" in ids
    assert "bundle_name_shadowed" in ids
    kinds = {(item["kind"], item["name"]) for item in result.result["conflicts"]}
    assert ("package", "file-mcp") in kinds
    assert ("bundle", "minimal") in kinds
    package = next(item for item in result.result["conflicts"] if item["name"] == "file-mcp")
    assert package["sources"] == ["keystone-codes", "partner"]


def test_catalog_add_unreachable_retryable(tmp_path: Path) -> None:
    result = add_source(tmp_path, PARTNER, catalog_runner(fail=True), ref="v1.0.0")
    assert result.code == 3
    assert result.retryable is True
    assert result.error is not None
    assert result.error["id"] == "source_unreachable"
    assert SHA
