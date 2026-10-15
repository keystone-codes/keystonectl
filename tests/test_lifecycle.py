"""service remove, upgrade, rollback, eject, and adopt."""

from __future__ import annotations

import copy
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from keystonectl.answers import pkg_var
from keystonectl.lifecycle import (
    adopt_service,
    eject_service,
    prune_retired_names,
    remove_service,
    rollback_service,
    upgrade_service,
)
from keystonectl.lock import load_lock, save_lock
from keystonectl.output import SecretRegistry
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.sources import package_cache
from tests.support import catalog_text
from tests.support.deploylab import CLOCK, apply_package
from tests.support.resources import Lab, install_ok_probes, s3_provider, standard_manifests

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def _install(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    return root


def _ok() -> FakeCommandRunner:
    return FakeCommandRunner([([], lambda args, **_kwargs: CommandResult(tuple(args), 0, "", ""))])


def _remove(
    root: Path,
    name: str = "file-mcp",
    *,
    confirm: bool = True,
    sets: dict | None = None,
    runner: FakeCommandRunner | None = None,
    nginx_root: Path | None = None,
):
    merged = dict(sets or {})
    if confirm and pkg_var(name, "CONFIRM_REMOVE") not in merged:
        merged[pkg_var(name, "CONFIRM_REMOVE")] = "yes"
    return remove_service(
        root,
        name,
        runner=runner or _ok(),
        secrets=SecretRegistry(),
        sets=merged,
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
        nginx_root=nginx_root,
    )


def test_remove_requires_confirmation(tmp_path: Path) -> None:
    root = _install(tmp_path)
    result = _remove(root, confirm=False)
    assert result.code == 2
    assert result.missing[0]["kind"] == "confirm"
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE"


def test_remove_restarts_keystone_when_registry_dropped(tmp_path: Path) -> None:
    root = _install(tmp_path)
    restarts: list[tuple[str, ...]] = []

    def watch(args, **_kwargs):
        body = list(args)
        if len(body) >= 2 and body[0] == "docker" and body[1] == "compose":
            if "restart" in body:
                restarts.append(tuple(body))
            if "ps" in body and "--status" in body and "running" in body:
                return CommandResult(tuple(args), 0, "keystone-id\n", "")
            if "ps" in body and "--format" in body and "json" in body:
                row = '{"Service":"keystone","State":"running","Health":"healthy"}'
                return CommandResult(tuple(args), 0, row + "\n", "")
        return CommandResult(tuple(args), 0, "", "")

    result = _remove(
        root,
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"},
        runner=FakeCommandRunner([([], watch)]),
    )
    assert result.code == 0, result.error
    assert any("restart" in call and "keystone" in call for call in restarts)


def test_remove_all_containers_before_fragment(tmp_path: Path) -> None:
    root = _install(tmp_path)
    fragment = root / "packages" / "file-mcp.yml"
    seen: list[bool] = []

    def watch(args, **_kwargs):
        if "rm" in args and "-s" in args:
            seen.append(fragment.is_file())
            assert "file-mcp-mcp" in args
        return CommandResult(tuple(args), 0, "", "")

    result = _remove(
        root,
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"},
        runner=FakeCommandRunner([([], watch)]),
    )
    assert result.code == 0, result.error
    assert seen == [True]
    assert not fragment.exists()


def test_remove_cleans_registry_snapshot_env_vhost_lock(tmp_path: Path) -> None:
    root = _install(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    lock["services"][0]["public"] = {
        "state": "ready",
        "domain": "files.example.com",
        "tls": "manual",
        "host_port": 47600,
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    nginx = tmp_path / "nginx"
    available = nginx / "sites-available"
    enabled = nginx / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir()
    site = available / "keystone-svc-file-mcp"
    site.write_text("server_name files.example.com;\n", encoding="utf-8")
    link = enabled / "keystone-svc-file-mcp"
    link.symlink_to(site)
    env = root / ".env"

    def host(args, **_kwargs):
        body = list(args)
        if tuple(body[:2]) == ("sudo", "-n"):
            body = body[2:]
        if body and body[0] == "rm":
            target = Path(body[-1])
            if target.is_symlink() or target.exists():
                target.unlink()
        return CommandResult(tuple(args), 0, "", "")

    env.write_text(
        "# >>> keystonectl: managed block, do not edit >>>\n"
        "KEYSTONE_MCP_UPSTREAM_FILES_BEARER=token\n"
        "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET=token\n"
        "KEEP_ME=1\n"
        "# <<< keystonectl <<<\n",
        encoding="utf-8",
    )
    result = _remove(
        root,
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"},
        runner=FakeCommandRunner([([], host)]),
        nginx_root=nginx,
    )
    assert result.code == 0, result.error
    registry = (root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8")
    assert "file-mcp" not in registry
    snapshot = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    assert snapshot["active"] == []
    assert not (root / "packages" / "file-mcp.env").exists()
    assert not site.exists()
    assert not link.exists()
    lock = load_lock(root / "services.lock.yaml")
    assert lock["services"] == []
    assert "KEEP_ME=1" in env.read_text(encoding="utf-8")
    assert "FILES_BEARER" not in env.read_text(encoding="utf-8")


def test_remove_keeps_override_and_says_so(tmp_path: Path) -> None:
    root = _install(tmp_path)
    override = root / "packages" / "file-mcp.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    result = _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"})
    assert result.code == 0, result.error
    assert override.is_file()
    assert "packages/file-mcp.override.yml is yours" in result.summary


def test_remove_ejected_keeps_operator_file(tmp_path: Path) -> None:
    root = _install(tmp_path)
    eject_service(root, "file-mcp", clock=lambda: CLOCK)
    fragment = root / "packages" / "file-mcp.yml"
    fragment.write_text("operator: true\n", encoding="utf-8")
    result = _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"})
    assert result.code == 0, result.error
    assert fragment.read_text(encoding="utf-8") == "operator: true\n"
    assert "packages/file-mcp.yml" in result.result["kept"]


def test_remove_result_lists_kept(tmp_path: Path) -> None:
    root = _install(tmp_path)
    override = root / "packages" / "file-mcp.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    lock["services"][0]["public"] = {
        "state": "ready",
        "domain": "files.example.com",
        "tls": "manual",
        "host_port": 47600,
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    result = _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"})
    assert result.code == 0, result.error
    kept = " ".join(result.result["kept"])
    assert "certificate files.example.com" in kept
    assert "packages/file-mcp.override.yml" in kept


def test_remove_required_by_refused(tmp_path: Path) -> None:
    root = _install(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    other = copy.deepcopy(lock["services"][0])
    other["name"] = "docs-service"
    other["package"] = "docs-service"
    other["fragment"] = "packages/docs-service.yml"
    other["registry_name"] = None
    other["namespace"] = None
    other["url"] = None
    other["tools"] = []
    other["env"] = {"files": [], "generated": [], "keystone": []}
    other["containers"]["mcp"]["service"] = "docs-service-mcp"
    lock["services"].append(other)
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    cache = package_cache(root, other["source"], "docs-service", other["version"])
    cache.mkdir(parents=True)
    (cache / "keystone-service.yaml").write_text(
        "name: docs-service\nrequires:\n  - file-mcp\n", encoding="utf-8"
    )
    result = _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"})
    assert result.code == 3
    assert result.error["id"] == "required_by"
    assert "docs-service" in result.error["message"]


def test_remove_policy_matched_names_move_to_reserved(tmp_path: Path) -> None:
    root = _install(tmp_path)
    policy = root / "policy" / "rbac-policy.yaml"
    policy.parent.mkdir()
    policy.write_text("roles:\n  user:\n    allow:\n      - files_*\n", encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    lock["layout"]["policy"] = {
        "file": "policy/rbac-policy.yaml",
        "container_path": "/etc/keystone/policy.yaml",
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    result = _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"})
    assert result.code == 0, result.error
    assert any(item["id"] == "policy_rows_orphaned" for item in result.warnings)
    snapshot = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    assert "files_create_upload_url" in snapshot["reserved"]
    retired = (root / ".keystonectl" / "retired-tools.txt").read_text(encoding="utf-8")
    assert "files_create_upload_url" in retired


def test_retired_names_dropped_when_policy_row_gone(tmp_path: Path) -> None:
    root = _install(tmp_path)
    policy = root / "policy" / "rbac-policy.yaml"
    policy.parent.mkdir()
    policy.write_text("roles:\n  user:\n    allow:\n      - files_*\n", encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    lock["layout"]["policy"] = {
        "file": "policy/rbac-policy.yaml",
        "container_path": "/etc/keystone/policy.yaml",
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    assert _remove(root, sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"}).code == 0
    policy.write_text("roles:\n  user:\n    allow:\n      - other_*\n", encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    dropped = prune_retired_names(root, lock)
    assert "files_create_upload_url" in dropped
    snapshot = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    assert "files_create_upload_url" not in snapshot.get("reserved", [])
    assert "files_create_upload_url" not in (root / ".keystonectl" / "retired-tools.txt").read_text(
        encoding="utf-8"
    )


def test_remove_provider_refused_with_consumers(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [s3_provider()])
    assert lab.apply("minio").code == 0
    lock = load_lock(lab.root / "services.lock.yaml")
    lock["resources"][0]["consumers"] = [
        {
            "service": "minio",
            "bucket": "files",
            "provisioned_at": "2026-10-13T00:00:00Z",
        }
    ]
    save_lock(lab.root / "services.lock.yaml", lock, now=CLOCK)
    result = remove_service(
        lab.root,
        "minio",
        runner=_ok(),
        secrets=SecretRegistry(),
        sets={"KEYSTONE_SETUP_SERVICE_MINIO_CONFIRM_REMOVE": "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert result.code == 3
    assert result.error["id"] == "resource_in_use"
    assert "still used by" in result.error["message"]


def test_remove_provider_keeps_volume_and_env(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [s3_provider()])
    assert lab.apply("minio").code == 0
    env = lab.root / "packages" / "minio.env"
    assert env.is_file()
    result = remove_service(
        lab.root,
        "minio",
        runner=_ok(),
        secrets=SecretRegistry(),
        sets={"KEYSTONE_SETUP_SERVICE_MINIO_CONFIRM_REMOVE": "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert result.code == 0, result.error
    assert env.is_file()
    assert "ks-pkg-minio-data" in result.result["kept"]


def test_remove_last_consumer_warns_provider_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    catalog = {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    assert lab.apply("minio").code == 0
    assert lab.apply("file-mcp", environ=catalog).code == 0
    assert lab.apply("pages-mcp", environ=catalog).code == 0
    first = _remove(lab.root, "file-mcp")
    assert first.code == 0, first.error
    assert not any(item["id"] == "provider_unused" for item in first.warnings)
    second = _remove(lab.root, "pages-mcp")
    assert second.code == 0, second.error
    unused = [item for item in second.warnings if item["id"] == "provider_unused"]
    assert len(unused) == 1
    assert "ks-pkg-minio-data" in unused[0]["message"]
    assert "minio" in unused[0]["message"]


def test_remove_provider_warns_data_kept(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [s3_provider()])
    assert lab.apply("minio").code == 0
    result = _remove(lab.root, "minio")
    assert result.code == 0, result.error
    kept = [item for item in result.warnings if item["id"] == "provider_data_kept"]
    assert len(kept) == 1
    assert "ks-pkg-minio-data" in kept[0]["message"]
    assert "ks-pkg-minio-data" in result.result["kept"]


def test_remove_last_external_consumer_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    assert lab.apply("file-mcp", environ=lab.s3_answers()).code == 0
    result = _remove(lab.root, "file-mcp")
    assert result.code == 0, result.error
    assert not any(item["id"] == "provider_unused" for item in result.warnings)


def test_upgrade_provider_with_consumers_keeps_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    catalog = {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    assert lab.apply("minio").code == 0
    assert lab.apply("file-mcp", environ=catalog).code == 0
    assert lab.apply("pages-mcp", environ=catalog).code == 0
    lock = load_lock(lab.root / "services.lock.yaml")
    resources_before = copy.deepcopy(lock["resources"])
    minio = next(item for item in lock["services"] if item["name"] == "minio")
    source = str(minio["source"])
    catalog_path = lab.root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml"
    catalog_doc = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog_doc["packages"]["minio"]["versions"].append({"version": "1.0.1", "ref": "v1.0.1"})
    catalog_text = yaml.safe_dump(catalog_doc, sort_keys=False)
    catalog_path.write_text(catalog_text, encoding="utf-8")
    lab.by_url["https://example.com/catalog.git"] = catalog_text
    document = yaml.safe_load(lab.texts["minio"])
    document["version"] = "1.0.1"
    document["summary"] = "minio 1.0.1"
    minio_text = yaml.safe_dump(document, sort_keys=False)
    lab.by_url["https://example.com/minio.git"] = minio_text
    cache = package_cache(lab.root, source, "minio", "1.0.1")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "keystone-service.yaml").write_text(minio_text, encoding="utf-8")
    upgraded = upgrade_service(
        lab.root,
        "minio",
        runner=lab.runner,
        secrets=SecretRegistry(),
        sets={pkg_var("minio", "CONFIRM_UPGRADE"): "1.0.1"},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert upgraded.code == 0, upgraded.error
    lock = load_lock(lab.root / "services.lock.yaml")
    assert lock["resources"] == resources_before
    resource = next(item for item in lock["resources"] if item["kind"] == "s3")
    assert len(resource["consumers"]) == 2
    assert resource["provider"] == "minio"
    minio = next(item for item in lock["services"] if item["name"] == "minio")
    assert minio["previous"]["version"] == "1.0.0"


def _upgrade(root: Path, ref: str = "file-mcp", *, sets: dict | None = None, runner=None):
    return upgrade_service(
        root,
        ref,
        runner=runner or _ok(),
        secrets=SecretRegistry(),
        sets=sets or {},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
    )


def test_upgrade_up_to_date(tmp_path: Path) -> None:
    root = _install(tmp_path)
    runner = FakeCommandRunner()
    result = _upgrade(root, runner=runner)
    assert result.code == 0
    assert result.result["up_to_date"] is True
    assert runner.calls == []


def test_upgrade_refused_when_fragment_hash_differs(tmp_path: Path) -> None:
    root = _install(tmp_path)
    path = root / "packages" / "file-mcp.yml"
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    runner = FakeCommandRunner()
    result = _upgrade(root, runner=runner)
    assert result.code == 3
    assert result.error["id"] == "fragment_modified"
    assert runner.calls == []
    assert "eject" in result.error["hint"]


def _add_duplicate_catalog(root: Path, name: str = "e2e") -> None:
    sources_path = root / "sources.yaml"
    data = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    data["sources"].append(
        {
            "name": name,
            "url": f"https://example.com/{name}-catalog.git",
            "ref": "v1.0.0",
            "priority": 50,
        }
    )
    sources_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    cache = root / ".keystonectl" / "catalogs" / name
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "catalog.yaml").write_text(catalog_text(name), encoding="utf-8")


def _prepare_upgrade(root: Path) -> None:
    catalog_path = root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    versions = catalog["packages"]["file-mcp"]["versions"]
    versions.append({"version": "1.1.0", "ref": "v1.1.0", "keystone_mcp": ">=0.5.0,<2.0"})
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    document = yaml.safe_load(_facade())
    document["version"] = "1.1.0"
    document["mcp"]["tools"].append("list_files")
    document["containers"]["worker"] = {
        "image": "example:1",
        "port": 8082,
        "init": False,
    }
    lock = load_lock(root / "services.lock.yaml")
    source = lock["services"][0]["source"]
    cache = package_cache(root, source, "file-mcp", "1.1.0")
    cache.mkdir(parents=True)
    (cache / "keystone-service.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    policy = root / "policy" / "rbac-policy.yaml"
    policy.parent.mkdir(exist_ok=True)
    policy.write_text("roles:\n  user:\n    allow:\n      - files_*\n", encoding="utf-8")
    lock["layout"]["policy"] = {
        "file": "policy/rbac-policy.yaml",
        "container_path": "/etc/keystone/policy.yaml",
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)


def _apply_runner(root: Path) -> FakeCommandRunner:
    from tests.support import SHA
    from tests.support.deploylab import DockerBox

    catalog_local = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()
    e2e_catalog = root / ".keystonectl" / "catalogs" / "e2e" / "catalog.yaml"
    current = _facade()
    newer = (
        current.replace("version: '1.0.0'", "version: '1.1.0'")
        .replace('version: "1.0.0"', 'version: "1.1.0"')
        .replace("version: 1.0.0\n", 'version: "1.1.0"\n')
    )
    box = DockerBox()
    state = {"n": 0}

    def ls_remote(args, **_kwargs):
        tag = "v1.0.0"
        for item in args:
            if str(item).startswith("refs/tags/"):
                tag = str(item).removeprefix("refs/tags/")
        return CommandResult(tuple(args), 0, f"{SHA}\trefs/tags/{tag}\n", "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        url = str(args[-2])
        if "catalog" in url:
            if "e2e" in url and e2e_catalog.is_file():
                (dest / "catalog.yaml").write_text(
                    e2e_catalog.read_text(encoding="utf-8"), encoding="utf-8"
                )
            else:
                (dest / "catalog.yaml").write_text(catalog_local, encoding="utf-8")
        else:
            (dest / "keystone-service.yaml").write_text(newer, encoding="utf-8")
        (dest / ".git").mkdir(exist_ok=True)
        return CommandResult(tuple(args), 0, "", "")

    def docker(args, **kwargs):
        if "diff" in args:
            state["n"] += 1
            text = (
                "Only in live (unreachable by any role):\n  (none)\n\n"
                "Unmatched allow patterns:\n  (none)\n"
            )
            return CommandResult(tuple(args), 0, text, "")
        return box(args, **kwargs)

    ok = CommandResult((), 0, "", "")
    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "init"], lambda args, **_kwargs: ok),
            (["git", "fetch"], ok),
            (["git", "checkout"], ok),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
            (["docker"], docker),
        ]
    )


def _diff_runner() -> FakeCommandRunner:
    state = {"n": 0}

    def docker(args, **_kwargs):
        if "diff" not in args:
            return CommandResult(tuple(args), 0, "", "")
        state["n"] += 1
        if state["n"] == 1:
            text = (
                "Only in live (unreachable by any role):\n  (none)\n\n"
                "Unmatched allow patterns:\n  (none)\n"
            )
        else:
            text = (
                "Only in live (unreachable by any role):\n  - files_list_files\n\n"
                "Unmatched allow patterns:\n  - files_*\n"
            )
        return CommandResult(tuple(args), 0, text, "")

    return FakeCommandRunner([(["docker"], docker)])


def test_upgrade_diff_tools_containers_and_policy(tmp_path: Path) -> None:
    root = _install(tmp_path)
    _prepare_upgrade(root)
    result = _upgrade(root, runner=_diff_runner())
    assert result.code == 2
    assert result.missing[0]["kind"] == "confirm"
    assert "1.0.0 -> 1.1.0" in result.missing[0]["question"]
    diff = result.result["diff"]
    assert "list_files" in diff["tools_added"]
    assert "worker" in diff["containers_added"]
    assert diff["policy_rows_stop_matching"] == ["files_*"]
    assert diff["tools_unreachable"] == ["files_list_files"]


def test_upgrade_applies_with_installed_source_when_name_is_ambiguous(tmp_path: Path) -> None:
    root = _install(tmp_path)
    _add_duplicate_catalog(root)
    _prepare_upgrade(root)
    result = _upgrade(
        root,
        runner=_apply_runner(root),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.1.0"},
    )
    assert result.code == 0, result.error
    assert result.missing == []


def test_upgrade_confirmation_must_match_version(tmp_path: Path) -> None:
    root = _install(tmp_path)
    _prepare_upgrade(root)
    wrong = _upgrade(
        root,
        runner=_diff_runner(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.0.0"},
    )
    assert wrong.code == 2
    assert wrong.missing[0]["var"].endswith("CONFIRM_UPGRADE")
    right = _upgrade(
        root,
        runner=_apply_runner(root),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.1.0"},
    )
    assert right.code != 2


def test_upgrade_keeps_registry_name_namespace_and_override(tmp_path: Path) -> None:
    root = _install(tmp_path)
    before = load_lock(root / "services.lock.yaml")["services"][0]
    override = root / "packages" / "file-mcp.override.yml"
    override.write_text("services: {}\n# keep\n", encoding="utf-8")
    _prepare_upgrade(root)
    result = _upgrade(
        root,
        runner=_apply_runner(root),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.1.0"},
    )
    assert result.code == 0, result.error
    after = load_lock(root / "services.lock.yaml")["services"][0]
    assert after["registry_name"] == before["registry_name"]
    assert after["namespace"] == before["namespace"]
    assert override.read_text(encoding="utf-8").endswith("# keep\n")


def test_upgrade_writes_bak_and_previous(tmp_path: Path) -> None:
    root = _install(tmp_path)
    _prepare_upgrade(root)
    result = _upgrade(
        root,
        runner=_apply_runner(root),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.1.0"},
    )
    assert result.code == 0, result.error
    backup = root / "packages" / "file-mcp.yml.bak"
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    previous = load_lock(root / "services.lock.yaml")["services"][0]["previous"]
    assert previous["version"] == "1.0.0"


def test_upgrade_ejected_gives_manual_steps(tmp_path: Path) -> None:
    root = _install(tmp_path)
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    eject_service(root, "file-mcp", clock=lambda: CLOCK)
    _prepare_upgrade(root)
    result = _upgrade(
        root,
        runner=_diff_runner(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE": "1.1.0"},
    )
    assert result.code == 0, result.error
    assert any(step.startswith("worker:") for step in result.result["manual_steps"])
    assert (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8") == fragment


def test_rollback_restores_previous_and_hash(tmp_path: Path) -> None:
    root = _install(tmp_path)
    fragment = root / "packages" / "file-mcp.yml"
    original = fragment.read_bytes()
    backup = root / "packages" / "file-mcp.yml.bak"
    backup.write_bytes(original)
    os.chmod(backup, 0o600)
    fragment.write_text("services: {}\n# new\n", encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    entry = lock["services"][0]
    entry["previous"] = {
        "version": "0.9.0",
        "ref": "v0.9.0",
        "commit": entry["commit"],
        "manifest_sha256": entry["manifest_sha256"],
        "containers": entry["containers"],
        "fragment_sha256": entry["fragment_sha256"],
        "tools": list(entry["tools"]),
    }
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    result = rollback_service(root, "file-mcp", runner=_ok(), clock=lambda: CLOCK)
    assert result.code == 0, result.error
    assert fragment.read_bytes() == original
    restored = load_lock(root / "services.lock.yaml")["services"][0]
    assert restored["version"] == "0.9.0"
    assert restored["previous"] is None
    assert restored["fragment_sha256"] == entry["fragment_sha256"]


def test_rollback_without_previous_refused(tmp_path: Path) -> None:
    root = _install(tmp_path)
    result = rollback_service(root, "file-mcp", runner=_ok(), clock=lambda: CLOCK)
    assert result.code == 3
    assert result.error["id"] == "no_previous_version"


def test_eject_marks_ejected_and_apply_skips_fragment(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.plan import run_service
    from tests.support.deploylab import DockerBox, runner_for

    root = _install(tmp_path)
    result = eject_service(root, "file-mcp", clock=lambda: CLOCK)
    assert result.code == 0
    assert "adopt" in result.summary
    lock = load_lock(root / "services.lock.yaml")
    assert lock["services"][0]["managed"] == "ejected"
    assert lock["services"][0]["fragment_sha256"] is None
    fragment = root / "packages" / "file-mcp.yml"
    fragment.write_text(fragment.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    marked = fragment.read_text(encoding="utf-8")
    text = _facade()
    catalog = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()
    applied = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert applied.code == 0, applied.error
    assert fragment.read_text(encoding="utf-8") == marked
    assert any(
        action["id"] == "deploy.fragment" and action["status"] == "skipped"
        for action in applied.actions
    )


def test_eject_remote_refused(tmp_path: Path) -> None:
    root = _install(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    remote = {
        "name": "remote-svc",
        "role": "service",
        "managed": "remote",
        "registry_name": "remote-svc",
        "namespace": "remote",
        "url": "http://remote.example/mcp",
    }
    lock["services"].append(remote)
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    result = eject_service(root, "remote-svc", clock=lambda: datetime(2026, 10, 13, tzinfo=UTC))
    assert result.code == 3
    assert result.error["id"] == "not_ejectable"


def test_adopt_requires_confirmation(tmp_path: Path) -> None:
    root = _install(tmp_path)
    eject_service(root, "file-mcp", clock=lambda: CLOCK)
    result = adopt_service(
        root,
        "file-mcp",
        runner=_ok(),
        sets={},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
        json_mode=True,
    )
    assert result.code == 2
    assert result.missing[0]["kind"] == "confirm"
    assert result.missing[0]["var"].endswith("CONFIRM_ADOPT")


def test_adopt_json_summary_has_no_values(tmp_path: Path) -> None:
    root = _install(tmp_path)
    eject_service(root, "file-mcp", clock=lambda: CLOCK)
    fragment = root / "packages" / "file-mcp.yml"
    fragment.write_text(
        "services:\n  mcp:\n    environment:\n      TOKEN: SECRETTOKEN\n", encoding="utf-8"
    )
    result = adopt_service(
        root,
        "file-mcp",
        runner=_ok(),
        sets={},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
        json_mode=True,
    )
    assert result.code == 2
    blob = result.summary + "\n" + yaml.safe_dump(result.result)
    assert "SECRETTOKEN" not in blob
    assert all("keys" in item for item in result.result["changes"])


def test_adopt_backs_up_and_regenerates(tmp_path: Path) -> None:
    root = _install(tmp_path)
    eject_service(root, "file-mcp", clock=lambda: CLOCK)
    fragment = root / "packages" / "file-mcp.yml"
    fragment.write_text("services: {}\n# operator\n", encoding="utf-8")
    result = adopt_service(
        root,
        "file-mcp",
        runner=_ok(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_ADOPT": "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
        json_mode=False,
    )
    assert result.code == 0, result.error
    backup = root / "packages" / "file-mcp.yml.ejected.bak"
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "operator" in backup.read_text(encoding="utf-8")
    assert "operator" not in fragment.read_text(encoding="utf-8")
    lock = load_lock(root / "services.lock.yaml")
    assert lock["services"][0]["managed"] == "generated"
    assert lock["services"][0]["fragment_sha256"]
    assert "ejected.bak" in result.summary
