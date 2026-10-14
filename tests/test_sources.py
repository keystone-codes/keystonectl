"""Package resolution, catalog add, and git fetch."""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
import yaml

from keystonectl.catalog import Source
from keystonectl.cli import main
from keystonectl.fetch import FetchError, fetch_tree
from keystonectl.plan import run_service
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.sources import add_source, refresh_source, resolve_package
from tests.support import SHA, catalog_text, git_runner, load_yaml, seed_catalog

URL = "https://example.com/catalog.git"


def _sources(*items: tuple[str, int]) -> list[Source]:
    return [
        Source(
            name, URL if name == "local" else f"https://example.com/{name}.git", "v1.0.0", priority
        )
        for name, priority in items
    ]


def _catalogs(names: list[str]) -> dict[str, dict]:
    found = {}
    for name in names:
        found[name] = yaml.safe_load(catalog_text(name))
    return found


def test_resolve_unique_name() -> None:
    resolved = resolve_package(
        "file-mcp",
        sources=_sources(("local", 10)),
        catalogs=_catalogs(["local"]),
        lock={"services": []},
        keystone_version="0.5.0",
    )
    assert resolved.error_id is None
    assert resolved.package is not None
    assert resolved.package.name == "file-mcp"
    assert resolved.package.version == "1.1.0"


def test_resolve_ambiguous_returns_missing_argument() -> None:
    resolved = resolve_package(
        "file-mcp",
        sources=_sources(("zeta", 1), ("alpha", 100)),
        catalogs=_catalogs(["zeta", "alpha"]),
        lock={"services": []},
        keystone_version="0.5.0",
    )
    assert resolved.error_id == "package_ambiguous"
    assert resolved.missing[0]["var"] == "package"
    assert resolved.missing[0]["kind"] == "argument"
    assert resolved.missing[0]["choices"] == ["alpha/file-mcp", "zeta/file-mcp"]


def test_resolve_source_qualified() -> None:
    resolved = resolve_package(
        "zeta/file-mcp@1.0.0",
        sources=_sources(("zeta", 1), ("alpha", 100)),
        catalogs=_catalogs(["zeta", "alpha"]),
        lock={"services": []},
        keystone_version="0.5.0",
    )
    assert resolved.package is not None
    assert resolved.package.source_name == "zeta"
    assert resolved.package.version == "1.0.0"


def test_newest_compatible_version_selected() -> None:
    resolved = resolve_package(
        "file-mcp",
        sources=_sources(("local", 10)),
        catalogs=_catalogs(["local"]),
        lock={"services": []},
        keystone_version="0.5.0",
    )
    assert resolved.package is not None
    assert resolved.package.version == "1.1.0"


def test_explicit_version_not_found() -> None:
    resolved = resolve_package(
        "file-mcp@9.9.9",
        sources=_sources(("local", 10)),
        catalogs=_catalogs(["local"]),
        lock={"services": []},
        keystone_version="0.5.0",
    )
    assert resolved.error_id == "version_not_found"


def test_installed_version_kept_without_explicit_version() -> None:
    resolved = resolve_package(
        "file-mcp",
        sources=_sources(("local", 10)),
        catalogs=_catalogs(["local"]),
        lock={"services": [{"name": "file-mcp", "version": "1.0.0", "commit": SHA}]},
        keystone_version="0.5.0",
    )
    assert resolved.package is not None
    assert resolved.package.version == "1.0.0"


def test_catalog_add_validates_and_writes_backup(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(
        "schema_version: 1\n"
        "sources:\n"
        "  - name: kept\n"
        "    url: https://example.com/kept.git\n"
        "    ref: v1.0.0\n"
        "    priority: 100\n",
        encoding="utf-8",
    )
    result = add_source(tmp_path, URL, git_runner(), name="local", ref="v1.0.0", priority=50)
    assert result.code == 0
    assert (tmp_path / "sources.yaml.bak").is_file()
    document = load_yaml(tmp_path / "sources.yaml")
    assert {item["name"] for item in document["sources"]} == {"kept", "local"}


def test_catalog_add_refuses_http(tmp_path: Path) -> None:
    result = add_source(tmp_path, "http://example.com/catalog.git", git_runner())
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "source_invalid"


def test_catalog_add_file_url_warns_local_source(tmp_path: Path) -> None:
    result = add_source(
        tmp_path,
        "file:///tmp/catalog",
        git_runner(),
        name="local",
        ref="v1.0.0",
        priority=10,
    )
    assert result.code == 0
    assert any(item["id"] == "local_source" for item in result.warnings)


def test_catalog_add_duplicate_refused(tmp_path: Path) -> None:
    first = add_source(tmp_path, URL, git_runner(), name="local", ref="v1.0.0", priority=10)
    assert first.code == 0
    second = add_source(tmp_path, URL, git_runner(), name="other", ref="v1.0.0", priority=20)
    assert second.code == 3
    assert second.error is not None
    assert second.error["id"] == "source_exists"


def test_catalog_add_picks_newest_tag_when_no_ref(tmp_path: Path) -> None:
    result = add_source(tmp_path, URL, git_runner(), name="local", priority=10)
    assert result.code == 0
    assert result.result["ref"] == "v1.2.0"
    assert any(item["id"] == "catalog_ref_chosen" for item in result.warnings)


def test_catalog_add_priority_default_unique(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(
        "schema_version: 1\n"
        "sources:\n"
        "  - name: kept\n"
        "    url: https://example.com/kept.git\n"
        "    ref: v1.0.0\n"
        "    priority: 50\n",
        encoding="utf-8",
    )
    result = add_source(tmp_path, URL, git_runner(), name="local", ref="v1.0.0")
    assert result.code == 0
    assert result.result["priority"] == 40


def test_fetch_clones_tag_and_records_commit(tmp_path: Path) -> None:
    runner = git_runner()
    _tree, commit = fetch_tree(runner, URL, "v1.0.0", tmp_path)
    assert commit == SHA
    clone = next(call for call in runner.calls if call.args[1] == "clone")
    assert clone.args[:8] == (
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "v1.0.0",
        "--single-branch",
        "-c",
    )
    assert clone.args[8] == "advice.detachedHead=false"
    assert clone.args[9] == URL


def test_fetch_by_commit_sha(tmp_path: Path) -> None:
    runner = git_runner()
    _tree, commit = fetch_tree(runner, URL, SHA, tmp_path)
    assert commit == SHA
    assert any(call.args[:3] == ("git", "init", call.args[2]) for call in runner.calls)
    fetch = next(call for call in runner.calls if call.args[1] == "fetch")
    assert fetch.args == ("git", "fetch", "--depth", "1", URL, SHA)


def test_fetch_refuses_branch_ref(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        [(["git", "ls-remote"], CommandResult((), 0, f"{SHA}\trefs/heads/main\n", ""))]
    )
    with pytest.raises(Exception) as caught:
        fetch_tree(runner, URL, "main", tmp_path)
    assert caught.value.error_id == "tag_not_found"  # type: ignore[attr-defined]


def test_tag_moved_refused(tmp_path: Path) -> None:
    seed_catalog(tmp_path)
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
    (tmp_path / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    runner = git_runner()
    from keystonectl.output import SecretRegistry

    result = run_service(
        tmp_path,
        "apply",
        "file-mcp@1.0.0",
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
    )
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "tag_moved"
    assert result.retryable is False


def test_catalog_cached_on_network_failure(tmp_path: Path) -> None:
    seed_catalog(tmp_path)
    source = Source("local", URL, "v1.0.0", 100)
    runner = FakeCommandRunner([(["git", "ls-remote"], CommandResult((), 1, "", "down"))])
    _path, noted, error_id, _message = refresh_source(tmp_path, source, runner)
    assert error_id is None
    assert noted is not None
    assert noted["id"] == "catalog_cached"


def test_tag_not_found_names_source_tag_and_url(tmp_path: Path) -> None:
    url = "https://user:token123@example.com/catalog.git"
    source = Source("keystone-codes", url, "v1.0.0", 100)
    runner = FakeCommandRunner([(["git", "ls-remote"], CommandResult((), 0, "", ""))])
    _path, _noted, error_id, message = refresh_source(tmp_path, source, runner)
    assert error_id == "tag_not_found"
    assert message is not None
    assert "keystone-codes" in message
    assert "v1.0.0" in message
    assert "example.com/catalog.git" in message
    assert "user:token123@" not in message
    assert "token123" not in message


def test_git_never_prompts(tmp_path: Path) -> None:
    runner = git_runner()
    fetch_tree(runner, URL, "v1.0.0", tmp_path)
    assert runner.calls
    for call in runner.calls:
        assert call.env is not None
        assert call.env["GIT_TERMINAL_PROMPT"] == "0"
        assert call.env["GIT_ASKPASS"] == "/bin/true"


def test_catalog_list_command_uses_fake_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("keystonectl.cli.build_runner", git_runner)
    code = main(["--json", "--root", str(tmp_path), "catalog", "list"])
    document = json.loads(capsys.readouterr().out)
    assert code == 0
    assert document["result"]["packages"]


def test_fetch_tmp_dir_survives_exdev_on_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone used to land in the system temp dir; renaming across file systems

    raises ``EXDEV`` when the deployment root is on a different one. The clone must
    land under ``<root>/.keystonectl/tmp`` so the later rename never crosses a mount
    point.
    """
    cache_root = tmp_path / ".keystonectl"
    real_rename = Path.rename

    def fake_rename(self: Path, target: str | Path) -> Path:
        target_path = Path(target)
        if not (self.is_relative_to(cache_root) and target_path.is_relative_to(cache_root)):
            raise OSError(errno.EXDEV, "cross-device link", str(self))
        return real_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", fake_rename, raising=True)
    runner = git_runner()
    result = add_source(tmp_path, URL, runner, name="local", ref="v1.0.0")
    assert result.code == 0
    assert (cache_root / "catalogs" / "local" / "catalog.yaml").is_file()


def test_fetch_tree_removes_tmp_dir_on_clone_failure(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        [
            (
                ["git", "ls-remote", "--tags"],
                CommandResult((), 0, f"{SHA}\trefs/tags/v1.0.0\n", ""),
            ),
            (["git", "clone"], CommandResult((), 1, "", "clone failed")),
        ]
    )
    with pytest.raises(FetchError):
        fetch_tree(runner, URL, "v1.0.0", tmp_path)
    tmp_root = tmp_path / ".keystonectl" / "tmp"
    assert tmp_root.is_dir()
    assert list(tmp_root.iterdir()) == []


def test_fetch_tmp_root_created_with_private_mode(tmp_path: Path) -> None:
    runner = git_runner()
    tree, _commit = fetch_tree(runner, URL, "v1.0.0", tmp_path)
    assert tree.is_relative_to(tmp_path / ".keystonectl" / "tmp")
    mode = (tmp_path / ".keystonectl" / "tmp").stat().st_mode & 0o777
    assert mode == 0o700


def test_fetch_creates_keystonectl_dir_on_bare_root(tmp_path: Path) -> None:
    """``catalog list --root`` on a checkout without ``init`` must not leave

    ``.keystonectl`` with a permissive, default mode: the deployment layout promises
    mode 700 and a ``.gitignore`` with a single ``*`` line for that directory.
    """
    assert not (tmp_path / ".keystonectl").exists()
    runner = git_runner()
    fetch_tree(runner, URL, "v1.0.0", tmp_path)
    keystonectl_dir = tmp_path / ".keystonectl"
    mode = keystonectl_dir.stat().st_mode & 0o777
    assert mode == 0o700
    assert (keystonectl_dir / ".gitignore").read_text() == "*\n"
