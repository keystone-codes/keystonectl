"""Packages of the deployment's own ``local/`` folder, without git (ADR 0011)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from keystonectl.answers import pkg_var
from keystonectl.check import run_check
from keystonectl.cli import main
from keystonectl.generate.install import ComposeDeployer
from keystonectl.lifecycle import remove_service, rollback_service, upgrade_service
from keystonectl.lock import LockError, load_lock, save_lock, validate_lock
from keystonectl.output import SecretRegistry
from keystonectl.package_dirs import MAX_BYTES, MAX_FILES, subtree_digest
from keystonectl.plan import run_service
from keystonectl.resources import schema_text
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.sources import add_source, list_catalogs
from tests.support import file_hashes, mode
from tests.support.deploylab import CLOCK, DockerBox, prepare_root, runner_for

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"
PROVIDER = {"provider/provider.yaml": "name: demo\n", "provider/lib/requirements.txt": "x==1\n"}
ENVIRON = {"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"}
OUTPUT = Draft202012Validator(json.loads(schema_text("output.schema.json")))
_MKFIFO = getattr(os, "mkfifo", None)


def _document(*, version: str = "1.0.0", name: str = "file-mcp") -> dict:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["name"] = name
    document["version"] = version
    mcp = document["containers"]["mcp"]
    mcp.pop("env", None)
    mcp["package_dir"] = {"from": "provider", "path": "/srv/provider"}
    return document


def _write_local(
    root: Path,
    *,
    version: str = "1.0.0",
    files: dict[str, str] | None = None,
    folder: str = "file-mcp",
    name: str = "file-mcp",
) -> Path:
    path = root / "local" / folder
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    text = yaml.safe_dump(_document(version=version, name=name), sort_keys=False)
    (path / "keystone-service.yaml").write_text(text, encoding="utf-8")
    for relative, body in (PROVIDER if files is None else files).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return path


def _root(tmp_path: Path, *, git_packages: tuple[str, ...] = ("docs-service",)) -> Path:
    """A deployment with a git source ``lab`` and a ``local/`` folder."""

    root = tmp_path / "root"
    prepare_root(root, dict.fromkeys(git_packages, ""), source="lab")
    (root / "local").mkdir()
    return root


def _catalog(root: Path) -> str:
    return (root / ".keystonectl" / "catalogs" / "lab" / "catalog.yaml").read_text()


def _runner(root: Path, box: DockerBox | None = None) -> FakeCommandRunner:
    return runner_for({}, box or DockerBox(), _catalog(root))


def _apply(root: Path, target: str = "local/file-mcp", box: DockerBox | None = None):
    return run_service(
        root,
        "apply",
        target,
        runner=_runner(root, box),
        secrets=SecretRegistry(),
        sets={},
        environ=ENVIRON,
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        defer_public=True,
    )


def _upgrade(root: Path, version: str):
    return upgrade_service(
        root,
        "file-mcp",
        runner=_runner(root),
        secrets=SecretRegistry(),
        sets={pkg_var("file-mcp", "CONFIRM_UPGRADE"): version},
        environ=ENVIRON,
        interactive=False,
        clock=lambda: CLOCK,
    )


def _entry(root: Path) -> dict:
    return load_lock(root / "services.lock.yaml")["services"][0]


def _ok() -> FakeCommandRunner:
    return FakeCommandRunner([([], lambda args, **_k: CommandResult(tuple(args), 0, "", ""))])


def _installed(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    _write_local(root)
    result = _apply(root)
    assert result.code == 0, result.error
    return root


# catalog list, catalog add, sources.yaml


def test_catalog_list_sees_local_package(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root)
    result = list_catalogs(
        root, _runner(root), keystone_version="0.5.0", lock=load_lock(root / "services.lock.yaml")
    )
    assert result.code == 0, result.error
    OUTPUT.validate(result.to_dict())
    package = next(item for item in result.result["packages"] if item["source"] == "local")
    assert package["name"] == "file-mcp"
    assert package["kind"] == "service"
    assert package["installed"] is None
    assert package["versions"] == [
        {"version": "1.0.0", "ref": "local", "keystone_mcp": ">=0.5.0,<2", "compatible": True}
    ]
    assert [item["name"] for item in result.result["sources"]] == ["lab"]


def test_catalog_list_without_local_folder_has_no_local_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    prepare_root(root, {"docs-service": ""}, source="lab")
    result = list_catalogs(
        root, _runner(root), keystone_version="0.5.0", lock=load_lock(root / "services.lock.yaml")
    )
    assert result.code == 0, result.error
    assert {item["source"] for item in result.result["packages"]} == {"lab"}


def test_catalog_add_name_local_is_reserved(tmp_path: Path) -> None:
    runner = FakeCommandRunner([])
    result = add_source(
        tmp_path, "https://example.com/other.git", runner, name="local", ref="v1.0.0"
    )
    assert result.code == 3
    assert result.error["id"] == "source_exists"
    assert "reserved for the local/ folder" in result.error["message"]
    assert runner.calls == []
    assert not (tmp_path / "sources.yaml").exists()


def test_catalog_add_catalog_named_local_is_refused(tmp_path: Path) -> None:
    from tests.support import git_runner

    result = add_source(tmp_path, "https://example.com/catalog.git", git_runner(), ref="v1.0.0")
    assert result.code == 3
    assert result.error["id"] == "source_exists"
    assert not (tmp_path / "sources.yaml").exists()


def test_sources_yaml_source_named_local_conflicts_with_folder(tmp_path: Path) -> None:
    root = tmp_path / "root"
    catalog = prepare_root(root, {"docs-service": ""})
    (root / "local").mkdir()
    result = list_catalogs(
        root,
        runner_for({}, DockerBox(), catalog),
        keystone_version="0.5.0",
        lock=load_lock(root / "services.lock.yaml"),
    )
    assert result.code == 3
    assert result.error["id"] == "source_invalid"
    assert "reserved" in result.error["message"]


# apply


def test_apply_installs_local_package_and_pins_content(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    before = file_hashes(root / "local")
    result = _apply(root)
    assert result.code == 0, result.error
    OUTPUT.validate(result.to_dict())
    digest = subtree_digest(root / "local", "file-mcp")
    assert result.result["commit"] is None
    assert result.result["content_sha256"] == digest
    entry = _entry(root)
    assert entry["source"] == "local"
    assert entry["ref"] == "local"
    assert entry["commit"] is None
    assert entry["version"] == "1.0.0"
    assert entry["content_sha256"] == digest
    copy = entry["package_dirs"]["mcp"]["copy"]
    assert (root / copy / "provider.yaml").read_text() == "name: demo\n"
    cache = root / ".keystonectl" / "packages" / "local" / "file-mcp" / "1.0.0"
    assert subtree_digest(cache, ".") == digest
    assert file_hashes(root / "local") == before
    assert folder.is_dir()
    assert list((root / ".keystonectl" / "tmp").iterdir()) == []
    assert run_check(root).code == 0


def test_apply_without_prefix_finds_the_only_local_package(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root)
    result = _apply(root, "file-mcp")
    assert result.code == 0, result.error
    assert _entry(root)["source"] == "local"


def test_apply_again_with_the_same_content_changes_nothing(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    before = load_lock(root / "services.lock.yaml")
    again = _apply(root)
    assert again.code == 0, again.error
    assert again.changes == 0
    assert load_lock(root / "services.lock.yaml")["services"] == before["services"]


def test_name_in_two_sources_is_ambiguous(tmp_path: Path) -> None:
    root = _root(tmp_path, git_packages=("file-mcp",))
    _write_local(root)
    result = _apply(root, "file-mcp")
    assert result.code == 2
    choices = result.missing[0]["choices"]
    assert choices == ["lab/file-mcp", "local/file-mcp"]


def test_folder_named_differently_from_manifest_is_invalid(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root, folder="other-mcp", name="file-mcp")
    result = _apply(root, "local/other-mcp")
    assert result.code == 3
    assert result.error["id"] == "manifest_invalid"
    assert "named after the package" in result.error["message"]


def test_explicit_other_version_is_not_found(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root)
    result = _apply(root, "local/file-mcp@2.0.0")
    assert result.code == 3
    assert result.error["id"] == "version_not_found"


# Tree rules: the same as package_dir (ADR 0010)


def _refused(root: Path) -> dict:
    result = _apply(root)
    assert result.code == 3
    assert result.error["id"] == "manifest_invalid"
    assert _no_service(root)
    assert not (root / "packages" / "file-mcp.yml").exists()
    return result.error


def _no_service(root: Path) -> bool:
    return load_lock(root / "services.lock.yaml")["services"] == []


def test_symlink_in_local_package_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    (folder / "provider" / "token").symlink_to("/etc/hostname")
    error = _refused(root)
    assert "symbolic link" in error["message"]


def test_symlinked_package_folder_is_not_a_package(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    _write_local(outside)
    (root / "local" / "file-mcp").symlink_to(outside / "local" / "file-mcp")
    listed = list_catalogs(
        root, _runner(root), keystone_version="0.5.0", lock=load_lock(root / "services.lock.yaml")
    )
    assert not any(item["source"] == "local" for item in listed.result["packages"])
    result = _apply(root)
    assert result.code == 3
    assert result.error["id"] == "package_not_found"


def test_symlinked_local_folder_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    prepare_root(root, {"docs-service": ""}, source="lab")
    outside = tmp_path / "outside"
    _write_local(outside)
    (root / "local").symlink_to(outside / "local")
    result = _apply(root)
    assert result.code == 3
    assert result.error["id"] == "package_not_found"


def test_symlinked_manifest_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    real = tmp_path / "manifest.yaml"
    (folder / "keystone-service.yaml").rename(real)
    (folder / "keystone-service.yaml").symlink_to(real)
    _refused(root)


def test_hard_link_in_local_package_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    os.link(folder / "provider" / "provider.yaml", tmp_path / "second-name")
    error = _refused(root)
    assert "hard link" in error["message"]


@pytest.mark.skipif(_MKFIFO is None, reason="no mkfifo on this platform")
def test_fifo_in_local_package_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    assert _MKFIFO is not None
    _MKFIFO(folder / "provider" / "pipe")
    error = _refused(root)
    assert "FIFO" in error["message"]


def test_local_package_over_size_limit_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root, files={**PROVIDER, "blob.bin": "x" * (MAX_BYTES + 1)})
    error = _refused(root)
    assert "the limit is" in error["message"]


def test_local_package_with_too_many_files_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    many = {f"data/{index}.txt": "x" for index in range(MAX_FILES + 1)}
    _write_local(root, files={**PROVIDER, **many})
    error = _refused(root)
    assert f"the limit is {MAX_FILES}" in error["message"]


# Changes under the same version, a newer one, an older one


def test_changed_content_without_new_version_is_local_changed(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    before = (root / "services.lock.yaml").read_bytes()
    _write_local(root, files={"provider/provider.yaml": "name: edited\n"})
    result = _apply(root)
    assert result.code == 3
    assert result.retryable is False
    assert result.error["id"] == "local_changed"
    assert "raise version in keystone-service.yaml" in result.error["hint"]
    assert (root / "services.lock.yaml").read_bytes() == before
    assert list((root / ".keystonectl" / "tmp").iterdir()) == []


def test_newer_version_goes_through_upgrade(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    first = _entry(root)["package_dirs"]["mcp"]["copy"]
    _write_local(root, version="1.1.0", files={"provider/provider.yaml": "name: v11\n"})
    applied = _apply(root)
    assert applied.code == 3
    assert applied.error["id"] == "version_not_found"
    assert "service upgrade file-mcp" in applied.error["hint"]

    upgraded = _upgrade(root, "1.1.0")
    assert upgraded.code == 0, upgraded.error
    assert upgraded.result["diff"]["from"] == "1.0.0"
    assert upgraded.result["diff"]["to"] == "1.1.0"
    entry = _entry(root)
    second = entry["package_dirs"]["mcp"]["copy"]
    assert second.startswith(".keystonectl/package-dirs/file-mcp/1.1.0-")
    assert (root / second / "provider.yaml").read_text() == "name: v11\n"
    assert entry["previous"]["package_dirs"]["mcp"]["copy"] == first
    assert entry["previous"]["version"] == "1.0.0"
    assert entry["previous"]["commit"] is None
    assert isinstance(entry["previous"]["content_sha256"], str)
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"../{second}:/srv/provider:ro" in fragment


def test_upgrade_is_up_to_date_when_the_version_did_not_move(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    result = _upgrade(root, "1.0.0")
    assert result.code == 0, result.error
    assert result.result["up_to_date"] is True


def test_older_version_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_local(root, version="1.2.0")
    assert _apply(root).code == 0
    _write_local(root, version="1.1.0")
    result = _apply(root)
    assert result.code == 3
    assert result.error["id"] == "version_not_found"
    assert "local package is older than the installed version" in result.error["message"]


def test_rollback_after_upgrade_does_not_read_local(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    first_entry = _entry(root)
    first = first_entry["package_dirs"]["mcp"]["copy"]
    _write_local(root, version="1.1.0", files={"provider/provider.yaml": "name: v11\n"})
    assert _upgrade(root, "1.1.0").code == 0
    second = _entry(root)["package_dirs"]["mcp"]["copy"]
    assert second != first

    shutil.rmtree(root / "local" / "file-mcp")
    (root / "local").chmod(0o000)
    try:
        runner = _ok()
        rolled = rollback_service(root, "file-mcp", runner=runner, clock=lambda: CLOCK)
    finally:
        (root / "local").chmod(0o755)
    assert rolled.code == 0, rolled.error
    entry = _entry(root)
    assert entry["previous"] is None
    assert entry["version"] == "1.0.0"
    assert entry["content_sha256"] == first_entry["content_sha256"]
    assert entry["package_dirs"]["mcp"]["copy"] == first
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"../{first}:/srv/provider:ro" in fragment
    assert (root / first / "provider.yaml").read_text() == "name: demo\n"
    ups = [call.args for call in runner.calls if "up" in call.args]
    assert ups and "--force-recreate" in ups[-1] and "file-mcp-mcp" in ups[-1]


# remove and check


def test_remove_leaves_the_local_folder(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    before = file_hashes(root / "local")
    removed = remove_service(
        root,
        "file-mcp",
        runner=_ok(),
        secrets=SecretRegistry(),
        sets={pkg_var("file-mcp", "CONFIRM_REMOVE"): "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert removed.code == 0, removed.error
    assert load_lock(root / "services.lock.yaml")["services"] == []
    assert file_hashes(root / "local") == before


def test_check_warns_when_local_folder_is_gone(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    checked = run_check(root)
    assert not any(item["id"] == "local_package_missing" for item in checked.warnings)
    shutil.rmtree(root / "local" / "file-mcp")
    checked = run_check(root)
    assert checked.code == 0
    warning = next(item for item in checked.warnings if item["id"] == "local_package_missing")
    assert warning["message"] == (
        "the folder local/file-mcp is gone; upgrade and reinstall need it, rollback does not"
    )


# init


def test_init_creates_an_empty_local_folder(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    assert main(["init", str(root)]) == 0
    assert (root / "local").is_dir()
    assert mode(root / "local") == 0o755
    assert list((root / "local").iterdir()) == []


def test_init_leaves_an_existing_local_folder_alone(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    assert main(["init", str(root)]) == 0
    (root / "local").chmod(0o750)
    _write_local(root)
    before = file_hashes(root / "local")
    assert main(["init", str(root)]) == 0
    assert mode(root / "local") == 0o750
    assert file_hashes(root / "local") == before


def test_init_adopt_creates_local_folder_when_missing(tmp_path: Path) -> None:
    from tests.support import FIXTURES

    root = tmp_path / "production"
    shutil.copytree(FIXTURES / "keystone-mcp-production", root)
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n"
        "KEYSTONE_MCP_RBAC_POLICY_PATH=/etc/keystone-mcp/policy/rbac-policy.yaml\n",
        encoding="utf-8",
    )
    assert not (root / "local").exists()
    assert main(["init", "--adopt", str(root)]) == 0
    assert mode(root / "local") == 0o755


# Lock schema


def _local_entry(tmp_path: Path) -> tuple[Path, dict]:
    root = _installed(tmp_path)
    return root, load_lock(root / "services.lock.yaml")


def test_lock_requires_content_sha256_for_local(tmp_path: Path) -> None:
    root, lock = _local_entry(tmp_path)
    lock["services"][0].pop("content_sha256")
    assert any("content_sha256" in item for item in validate_lock(lock))
    with pytest.raises(LockError):
        save_lock(root / "services.lock.yaml", lock, now=CLOCK)


def test_lock_still_requires_commit_for_git_sources(tmp_path: Path) -> None:
    _root_path, lock = _local_entry(tmp_path)
    entry = lock["services"][0]
    entry["source"] = "lab"
    entry["ref"] = "v1.0.0"
    entry.pop("content_sha256")
    assert any("commit is required" in item for item in validate_lock(lock))
    entry["commit"] = "a" * 40
    assert validate_lock(lock) == []


@pytest.mark.xfail(
    reason="G10: _ensure_service does not record the new version and pins after upgrade",
    strict=False,
)
def test_upgrade_records_the_new_version_and_content(tmp_path: Path) -> None:
    root = _installed(tmp_path)
    old = _entry(root)["content_sha256"]
    _write_local(root, version="1.1.0", files={"provider/provider.yaml": "name: v11\n"})
    assert _upgrade(root, "1.1.0").code == 0
    entry = _entry(root)
    assert entry["version"] == "1.1.0"
    assert entry["content_sha256"] == subtree_digest(root / "local", "file-mcp")
    assert entry["content_sha256"] != old


@pytest.mark.skipif(_MKFIFO is None, reason="no mkfifo on this platform")
def test_fifo_in_place_of_the_manifest_does_not_hang(tmp_path: Path) -> None:
    root = _root(tmp_path)
    folder = _write_local(root)
    (folder / "keystone-service.yaml").unlink()
    assert _MKFIFO is not None
    _MKFIFO(folder / "keystone-service.yaml")
    listed = list_catalogs(
        root, _runner(root), keystone_version="0.5.0", lock=load_lock(root / "services.lock.yaml")
    )
    package = next(item for item in listed.result["packages"] if item["source"] == "local")
    assert package["versions"] == []
    _refused(root)
