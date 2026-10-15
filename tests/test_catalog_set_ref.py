"""catalog set-ref moves one source pin and leaves the others alone."""

from __future__ import annotations

from pathlib import Path

import yaml

from keystonectl.catalog import DEFAULT_SOURCES
from keystonectl.sources import set_source_ref
from tests.support import mode
from tests.support.served_catalog import SHA, catalog_runner

PARTNER = "https://example.com/partner.git"
CODES = "https://example.com/keystone-codes.git"
TAGS = f"{SHA}\trefs/tags/v1.0.0\n{SHA}\trefs/tags/v1.1.0\n{SHA}\trefs/tags/v1.2.0\n"


class _NoGit:
    def run(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("git must not be called")


def _sources(root: Path, rows: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "sources": rows}
    (root / "sources.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _pair() -> list[dict[str, object]]:
    return [
        {"name": "keystone-codes", "url": CODES, "ref": "v1.0.0", "priority": 100},
        {"name": "partner", "url": PARTNER, "ref": "v1.0.0", "priority": 50},
    ]


def test_set_ref_moves_only_that_source(tmp_path: Path) -> None:
    _sources(tmp_path, _pair())
    cache = tmp_path / ".keystonectl" / "catalogs" / "keystone-codes"
    cache.mkdir(parents=True)
    (cache / "stale.txt").write_text("old\n", encoding="utf-8")
    result = set_source_ref(tmp_path, "keystone-codes", "v1.1.0", catalog_runner(tags=TAGS))
    assert result.code == 0, result.error
    assert result.command == "catalog set-ref"
    assert result.changes == 1
    assert result.result["previous_ref"] == "v1.0.0"
    assert result.result["ref"] == "v1.1.0"
    assert result.result["packages"] == 4
    document = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in document["sources"]}
    assert by_name["keystone-codes"]["ref"] == "v1.1.0"
    assert by_name["partner"] == {
        "name": "partner",
        "url": PARTNER,
        "ref": "v1.0.0",
        "priority": 50,
    }
    backup = tmp_path / "sources.yaml.bak"
    assert backup.is_file()
    assert mode(tmp_path / "sources.yaml") == 0o600
    assert mode(backup) == 0o600
    assert not (cache / "stale.txt").exists()
    catalog = (cache / "catalog.yaml").read_text(encoding="utf-8")
    assert "file-mcp" in catalog


def test_set_ref_without_sources_file_writes_defaults(tmp_path: Path) -> None:
    result = set_source_ref(tmp_path, "keystone-codes", "v1.1.0", catalog_runner(tags=TAGS))
    assert result.code == 0, result.error
    document = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert document["sources"] == [
        {
            "name": DEFAULT_SOURCES[0].name,
            "url": DEFAULT_SOURCES[0].url,
            "ref": "v1.1.0",
            "priority": DEFAULT_SOURCES[0].priority,
        }
    ]
    assert mode(tmp_path / "sources.yaml") == 0o600
    assert not (tmp_path / "sources.yaml.bak").exists()


def test_set_ref_same_ref_is_unchanged(tmp_path: Path) -> None:
    _sources(tmp_path, _pair())
    before = (tmp_path / "sources.yaml").read_bytes()
    result = set_source_ref(tmp_path, "keystone-codes", "v1.0.0", _NoGit())  # type: ignore[arg-type]
    assert result.code == 0, result.error
    assert result.changes == 0
    assert result.command == "catalog set-ref"
    assert (tmp_path / "sources.yaml").read_bytes() == before
    assert not (tmp_path / "sources.yaml.bak").exists()


def test_set_ref_unknown_source(tmp_path: Path) -> None:
    _sources(tmp_path, _pair())
    before = (tmp_path / "sources.yaml").read_bytes()
    result = set_source_ref(tmp_path, "nope", "v1.1.0", _NoGit())  # type: ignore[arg-type]
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "source_not_found"
    assert result.retryable is False
    assert result.command == "catalog set-ref"
    assert result.error["message"] == "No source named nope. Sources: keystone-codes, partner."
    assert (tmp_path / "sources.yaml").read_bytes() == before
    assert not (tmp_path / "sources.yaml.bak").exists()


def test_set_ref_missing_tag(tmp_path: Path) -> None:
    _sources(tmp_path, _pair()[:1])
    before = (tmp_path / "sources.yaml").read_bytes()
    result = set_source_ref(tmp_path, "keystone-codes", "v9.9.9", catalog_runner(tags=TAGS))
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "tag_not_found"
    assert "v9.9.9" in result.error["message"]
    assert CODES in result.error["message"]
    assert (tmp_path / "sources.yaml").read_bytes() == before
    assert not (tmp_path / "sources.yaml.bak").exists()


def test_set_ref_warns_installed_version_missing(tmp_path: Path) -> None:
    _sources(tmp_path, _pair()[:1])
    lock = {
        "schema_version": 1,
        "layout_version": 1,
        "updated_at": "2026-10-13T00:00:00Z",
        "updated_by": "keystonectl 0.1.0",
        "layout": None,
        "services": [
            {
                "name": "file-mcp",
                "role": "service",
                "managed": "generated",
                "source": "keystone-codes",
                "package": "file-mcp",
                "version": "1.0.0",
                "ref": "v1.0.0",
                "commit": "b" * 40,
                "manifest_sha256": "d" * 64,
                "fragment": "packages/file-mcp.yml",
                "fragment_sha256": "e" * 64,
                "containers": {
                    "mcp": {
                        "service": "file-mcp-mcp",
                        "image": "example:1",
                        "init": False,
                    }
                },
            }
        ],
        "resources": [],
    }
    lock_path = tmp_path / "services.lock.yaml"
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    before_lock = lock_path.read_bytes()
    # The served catalog lists file-mcp 1.0.0. Serve a catalog that dropped it.
    body = """\
schema_version: 1
name: keystone-codes
packages:
  file-mcp:
    kind: service
    summary: Upload and download links for large files.
    source: https://example.com/keystone-codes/file-mcp.git
    versions:
      - version: 2.0.0
        ref: v2.0.0
        keystone_mcp: ">=0.5.0,<2.0"
"""
    result = set_source_ref(tmp_path, "keystone-codes", "v1.1.0", _catalog_text_runner(body))
    assert result.code == 0, result.error
    assert lock_path.read_bytes() == before_lock
    messages = [
        item["message"] for item in result.warnings if item["id"] == "installed_version_missing"
    ]
    expected = (
        "file-mcp 1.0.0 is installed from keystone-codes, "
        "and keystone-codes v1.1.0 no longer lists it; "
        "upgrade or apply will need another version"
    )
    assert messages == [expected]


def _catalog_text_runner(catalog_text: str):
    from keystonectl.runner import CommandResult, FakeCommandRunner

    listing = f"{SHA}\trefs/tags/v1.1.0\n"

    def ls_remote(args, **_kwargs):
        last = str(args[-1])
        if last.startswith("refs/tags/"):
            tag = last.removeprefix("refs/tags/")
            if f"refs/tags/{tag}" not in listing:
                return CommandResult(tuple(args), 0, "", "")
            return CommandResult(tuple(args), 0, f"{SHA}\trefs/tags/{tag}\n", "")
        return CommandResult(tuple(args), 0, listing, "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "catalog.yaml").write_text(catalog_text, encoding="utf-8")
        (dest / ".git").mkdir(exist_ok=True)
        return CommandResult(tuple(args), 0, "", "")

    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
        ]
    )
