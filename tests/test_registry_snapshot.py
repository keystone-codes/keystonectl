"""Registry entries, namespaces, and the catalog snapshot."""

from __future__ import annotations

from pathlib import Path

import yaml

from keystonectl.generate.registry import registry_url
from tests.support.deploylab import DockerBox, apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"
GOLDEN = Path(__file__).resolve().parent / "data" / "golden"


def _load(name: str) -> dict:
    return yaml.safe_load((PACKAGES / name / "keystone-service.yaml").read_text())


def _facade(**changes: object) -> str:
    document = _load("file-mcp")
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    mcp = document["mcp"]
    for key, value in changes.items():
        if key == "name":
            document["name"] = value
        else:
            mcp[key] = value
    return yaml.safe_dump(document)


def _registry(root: Path) -> dict:
    return yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text())


def test_registry_entry_added_others_preserved(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    document = _registry(root)
    foreign = {
        "name": "foreign",
        "url": "http://foreign:1/mcp",
        "namespace": "other",
        "enabled": True,
    }
    document["entries"].insert(0, foreign)
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    again, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    names = [item["name"] for item in _registry(root)["entries"]]
    assert names[0] == "foreign"
    assert "file-mcp" in names


def test_registry_url_uses_mcp_container_service_name() -> None:
    manifest = _load("file-mcp")
    assert registry_url(manifest) == "http://file-mcp-mcp:8081/mcp"


def test_registry_backup_written(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert (root / "keystonectl" / "upstreams.yaml.bak").is_file()


def test_registry_name_fixed_on_reinstall(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    assert lock["services"][0]["registry_name"] == "file-mcp"
    again, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    assert lock["services"][0]["registry_name"] == "file-mcp"


def test_registry_name_taken_by_foreign_entry(tmp_path: Path) -> None:
    root = tmp_path / "root"
    prepared = tmp_path / "prep"
    apply_package(prepared, "file-mcp", {"file-mcp": _facade()})
    # A foreign file entry with the same name, and no lock ownership, is taken.
    from tests.support.deploylab import prepare_root

    text = _facade()
    prepare_root(root, {"file-mcp": text})
    (root / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries:\n  - name: file-mcp\n    url: http://other:1/mcp\n"
        "    namespace: other\n    enabled: true\n",
        encoding="utf-8",
    )
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import runner_for

    catalog = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()
    box = DockerBox()
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, box, catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 3
    assert result.error["id"] == "registry_name_taken"


def test_namespace_default_from_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    assert result.result["namespace"] == "files"


def test_namespace_collision_asks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    text = _facade()
    catalog = prepare_root(root, {"file-mcp": text})
    (root / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries:\n  - name: other\n    namespace: files\n    url: http://other:1/mcp\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 2
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE"


def test_namespace_builtin_prefix_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(
        root,
        "file-mcp",
        {"file-mcp": _facade()},
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE": "users"},
    )
    assert result.code == 2
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE"


def test_namespace_pair_collision(tmp_path: Path) -> None:
    root = tmp_path / "root"
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    text = _facade(namespace="billing")
    catalog = prepare_root(root, {"file-mcp": text})
    (root / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries:\n  - name: other\n    namespace: billing_admin\n"
        "    url: http://other:1/mcp\n    enabled: true\n",
        encoding="utf-8",
    )
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 2


def test_env_name_normalization_collision(tmp_path: Path) -> None:
    root = tmp_path / "root"
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    text = _facade()
    catalog = prepare_root(root, {"file-mcp": text})
    (root / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries:\n  - name: other\n    namespace: my_ns\n"
        "    url: http://other:1/mcp\n    enabled: true\n    auth:\n"
        "      bearer_env: KEYSTONE_MCP_UPSTREAM_MY_NS_BEARER\n"
        "      identity_secret_env: KEYSTONE_MCP_UPSTREAM_MY_NS_IDENTITY_SECRET\n",
        encoding="utf-8",
    )
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE": "my-ns"},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 2


def test_namespace_change_unsupported(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    second, _runner, _box = apply_package(
        root,
        "file-mcp",
        {"file-mcp": _facade()},
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE": "other"},
        reset=False,
    )
    assert second.code == 3
    assert second.error["id"] == "namespace_change_unsupported"


def test_registry_full(tmp_path: Path) -> None:
    root = tmp_path / "root"
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    text = _facade()
    catalog = prepare_root(root, {"file-mcp": text})
    entries = [
        {
            "name": f"up-{index}",
            "namespace": f"ns{index}",
            "url": f"http://h{index}:1/mcp",
            "enabled": True,
        }
        for index in range(64)
    ]
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "entries": entries}, sort_keys=False),
        encoding="utf-8",
    )
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 3
    assert result.error["id"] == "registry_full"


def test_package_without_mcp_has_no_registry_snapshot_or_gateway_secrets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    text = (PACKAGES / "hello-worker" / "keystone-service.yaml").read_text()
    document = yaml.safe_load(text)
    document.pop("needs", None)
    result, _runner, _box = apply_package(
        root,
        "hello-worker",
        {"hello-worker": yaml.safe_dump(document)},
    )
    assert result.code == 0, result.error
    assert (
        "entries: []" in (root / "keystonectl" / "upstreams.yaml").read_text()
        or yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text())["entries"] == []
    )
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    entry = next(item for item in lock["services"] if item["name"] == "hello-worker")
    assert entry["namespace"] is None
    assert entry["registry_name"] is None
    assert not (root / "packages" / "hello-worker.mcp.env").exists()


def test_snapshot_active_union_keeps_foreign_names(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (tmp_path / "seed").mkdir()
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    snapshot = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    snapshot["active"].append("foreign_tool")
    (root / "keystonectl" / "catalog-snapshot.yaml").write_text(
        yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8"
    )
    again, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    active = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())["active"]
    assert "foreign_tool" in active
    assert "files_create_upload_url" in active


def test_snapshot_reserved_and_wip_preserved(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    (root / "keystonectl" / "catalog-snapshot.yaml").write_text(
        "active: []\nreserved:\n- keep_me\nwip:\n- soon\n", encoding="utf-8"
    )
    again, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    document = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    assert document["reserved"] == ["keep_me"]
    assert document["wip"] == ["soon"]


def test_snapshot_created_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    text = _facade()
    catalog = prepare_root(root, {"file-mcp": text})
    (root / "keystonectl" / "catalog-snapshot.yaml").unlink()
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 0, result.error
    assert (root / "keystonectl" / "catalog-snapshot.yaml").is_file()
    assert "files_create_upload_url" in (root / "keystonectl" / "catalog-snapshot.yaml").read_text()
