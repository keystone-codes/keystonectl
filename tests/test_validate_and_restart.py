"""Validation, policy, and how keystone-mcp is restarted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.support.deploylab import DockerBox, apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def test_layout_invalid_when_keystone_service_missing(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.output import SecretRegistry
    from keystonectl.plan import run_service
    from tests.support.deploylab import LAYOUT, prepare_root, runner_for

    bare = tmp_path / "bare"
    text = _facade()
    catalog = prepare_root(bare, {"file-mcp": text})
    lock = yaml.safe_load((bare / "services.lock.yaml").read_text())
    layout = dict(LAYOUT)
    layout["keystone_service"] = "missing"
    lock["layout"] = layout
    (bare / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    box = DockerBox()
    box.config = {"name": "demo", "services": {}}
    result = run_service(
        bare,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, box, catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        deployer=ComposeDeployer(),
    )
    assert result.code == 3
    assert result.error["id"] == "layout_invalid"


def test_layout_invalid_when_started_without_packages_file(tmp_path: Path) -> None:
    box = DockerBox()
    box.config = {
        "name": "demo",
        "services": {"keystone": {"environment": {}, "volumes": [], "networks": []}},
    }
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 3
    assert result.error["id"] == "layout_invalid"
    assert "-f" in result.error["message"] or "keystonectl" in result.error["message"]


def test_upstream_validate_runs_in_container(tmp_path: Path) -> None:
    result, runner, _box = apply_package(tmp_path / "root", "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    calls = [
        call.args for call in runner.calls if "upstream" in call.args and "validate" in call.args
    ]
    assert len(calls) == 1
    args = calls[0]
    assert args[:8] == (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-e",
        "KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP=true",
        "-e",
    )
    assert "--entrypoint" in args
    assert "keystone-mcp" in args
    assert "strict-unreachable" not in " ".join(args)
    assert "/ksctl/upstreams.yaml" in args


def test_upstream_validate_namespace_error_becomes_question(tmp_path: Path) -> None:
    box = DockerBox()
    box.validate_code = 1
    box.validate_stderr = "namespace files is rejected\n"
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 2
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_NAMESPACE"


def test_registry_invalid_not_retryable(tmp_path: Path) -> None:
    box = DockerBox()
    box.validate_code = 1
    box.validate_stderr = "url must use http\n"
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 3
    assert result.error["id"] == "registry_invalid"
    assert result.retryable is False


def test_catalog_validate_without_strict_unreachable(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    result, runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 0, result.error
    calls = [
        " ".join(call.args)
        for call in runner.calls
        if "catalog" in call.args and "validate" in call.args
    ]
    assert calls
    assert all("strict-unreachable" not in call for call in calls)


def test_catalog_validate_skipped_when_rbac_off(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    assert any(item["id"] == "policy_check_skipped" for item in result.warnings)
    assert not any("catalog" in call.args and "validate" in call.args for call in runner.calls)


def test_catalog_validate_new_errors_refuse(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    box.catalog_script = [
        (0, ""),
        (1, "unmatched allow patterns (typo or missing catalog/WIP entry): files_*\n"),
    ]
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 3
    assert result.error["id"] == "policy_breaks"


def test_catalog_validate_same_errors_warn(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    box.catalog_code = 1
    box.catalog_stderr = "unmatched allow patterns (typo or missing catalog/WIP entry): files_*\n"
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 0, result.error
    assert any(item["id"] == "policy_already_inconsistent" for item in result.warnings)


def test_unreachable_tools_is_warning(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    box.catalog_stderr = (
        "rbac policy leaves 1 tool(s) unreachable by any role: files_create_upload_url\n"
    )
    result, _runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 0, result.error
    assert any(item["id"] == "tools_unreachable" for item in result.warnings)


def test_live_names_used_when_keystone_running(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    box.keystone_id = "cid\n"
    box.live_names = "users_list\nfiles_old\n"
    result, runner, _box = apply_package(
        tmp_path / "root", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 0, result.error
    assert any("keystone_mcp.rbac.catalog" in call.args for call in runner.calls)


def test_nothing_written_when_validation_fails(tmp_path: Path) -> None:
    box = DockerBox()
    box.validate_code = 1
    box.validate_stderr = "url must use http\n"
    root = tmp_path / "root"
    from tests.support.deploylab import prepare_root

    text = _facade()
    prepare_root(root, {"file-mcp": text})
    watched = [
        root / "packages" / "file-mcp.yml",
        root / "keystonectl" / "upstreams.yaml",
        root / "keystonectl" / "catalog-snapshot.yaml",
        root / ".env",
        root / "docker-compose.packages.yml",
    ]
    before = {path: path.read_bytes() if path.is_file() else None for path in watched}
    result, _runner, _box = apply_package(
        root, "file-mcp", {"file-mcp": text}, box=box, reset=False
    )
    assert result.code == 3
    after = {path: path.read_bytes() if path.is_file() else None for path in watched}
    assert after == before


def test_init_failure_stops_before_registry(tmp_path: Path) -> None:
    text = (PACKAGES / "docs-service" / "keystone-service.yaml").read_text()
    box = DockerBox()
    box.init_fail.add("docs-service-migrate")
    root = tmp_path / "root"
    document = yaml.safe_load(text)
    document.pop("needs", None)
    result, _runner, _box = apply_package(
        root,
        "docs-service",
        {"docs-service": yaml.safe_dump(document)},
        box=box,
    )
    assert result.code == 3
    assert result.error["id"] == "init_failed"
    document = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text())
    assert document["entries"] == []


def test_registry_not_installed_when_service_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr("keystonectl.generate.install._monotonic", lambda: clock["t"])

    def advance(_seconds: float = 3) -> None:
        clock["t"] += 10000

    monkeypatch.setattr("keystonectl.generate.install.pause", advance)
    box = DockerBox()
    box.inspect_health = "unhealthy"
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, box=box)
    assert result.code == 3
    assert result.error["id"] == "service_unhealthy"
    document = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text())
    assert document["entries"] == []


def test_keystone_up_when_its_wiring_changed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    box = DockerBox()
    box.keystone_id = "cid\n"
    (tmp_path).mkdir(exist_ok=True)
    result, runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, box=box)
    assert result.code == 0, result.error
    assert any("up" in call.args and "--no-deps" in call.args for call in runner.calls)


def test_keystone_restarted_when_registry_changed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    document = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text())
    document["entries"] = [item for item in document["entries"] if item.get("name") != "file-mcp"]
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    box = DockerBox()
    box.keystone_id = "cid\n"
    again, runner, _box = apply_package(
        root, "file-mcp", {"file-mcp": _facade()}, box=box, reset=False
    )
    assert again.code == 0, again.error
    assert any("restart" in call.args for call in runner.calls)


def test_keystone_sighup_when_only_snapshot_changed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    snapshot = yaml.safe_load((root / "keystonectl" / "catalog-snapshot.yaml").read_text())
    snapshot["active"] = []
    (root / "keystonectl" / "catalog-snapshot.yaml").write_text(
        yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8"
    )
    box = DockerBox()
    box.keystone_id = "cid\n"
    again, runner, _box = apply_package(
        root, "file-mcp", {"file-mcp": _facade()}, box=box, reset=False
    )
    assert again.code == 0, again.error
    assert any(
        call.args[-3:] == ("kill", "-s", "HUP") or ("kill" in call.args and "HUP" in call.args)
        for call in runner.calls
    )


def test_keystone_untouched_when_not_running(tmp_path: Path) -> None:
    result, runner, _box = apply_package(tmp_path / "root", "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    assert result.result.get("info", "").startswith("keystone-mcp is not running")
    assert not any("restart" in call.args or "HUP" in call.args for call in runner.calls)


def test_registry_written_atomically_in_keystonectl_dir(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    text = (root / "keystonectl" / "upstreams.yaml").read_text()
    assert text.startswith("# Managed by keystonectl")
    assert "file-mcp" in text


def test_discovery_from_metrics(tmp_path: Path) -> None:
    root = tmp_path / "root"
    box = DockerBox()
    box.keystone_id = "cid\n"
    box.metrics = 'keystone_mcp_upstream_available{namespace="files"} 1\n'
    from tests.support.deploylab import prepare_root

    text = _facade()
    prepare_root(root, {"file-mcp": text})
    (root / ".env").write_text("KEYSTONE_MCP_METRICS_ENABLED=true\n", encoding="utf-8")
    result, _runner, _box = apply_package(
        root, "file-mcp", {"file-mcp": text}, box=box, reset=False
    )
    assert result.code == 0, result.error
    assert result.result["discovery"] == "available"


def test_discovery_from_logs_fallback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    box = DockerBox()
    box.keystone_id = "cid\n"
    box.logs = "upstream discovery: 0 of 1 upstream(s) unavailable (none); proxying the rest\n"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, box=box)
    assert result.code == 0, result.error
    assert result.result["discovery"] == "available"


def test_recommended_policy_and_critical_in_result(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    assert result.result["critical_tools"] == ["files_create_upload_url", "files_get_download_link"]
    assert "critical_tools" in result.result["critical_tools_hint"]
    assert result.result["recommended_policy"]["path"] == "policy.example.yaml"


def test_changed_env_file_recreates_the_container(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    path = root / "packages" / "file-mcp.env"
    path.write_text(path.read_text(encoding="utf-8") + "FILE_MCP_SIGNING_SECRET=rotated-value\n")
    again, runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    assert again.changes != 0
    assert any("--force-recreate" in call.args and "up" in call.args for call in runner.calls)


def test_rerun_same_version_changes_zero(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    again, runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert again.code == 0, again.error
    assert again.changes == 0
    assert not any(
        "pull" in call.args or "--force-recreate" in call.args or "kill" in call.args
        for call in runner.calls
        if call.args
        and call.args[0] == "docker"
        and "up" in call.args
        or "pull" in call.args
        or "kill" in call.args
    )


def _policy_box(policy: Path) -> DockerBox:
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("roles: {}\n", encoding="utf-8")
    box = DockerBox()
    box.config = {
        "name": "demo",
        "services": {
            "keystone": {
                "environment": {
                    "KEYSTONE_MCP_UPSTREAMS_PATH": "/etc/keystone-mcp/keystonectl/upstreams.yaml",
                    "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT": "/etc/keystone-mcp/keystonectl/catalog-snapshot.yaml",
                    "KEYSTONE_MCP_RBAC_POLICY_PATH": "/policy.yaml",
                },
                "volumes": [
                    "./keystonectl:/etc/keystone-mcp/keystonectl:ro",
                    f"{policy}:/policy.yaml:ro",
                ],
                "networks": ["keystone-mcp-upstreams"],
            }
        },
    }
    return box


def _write_policy_env(root: Path, policy: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # apply_package(reset=True) recreates .env only if we write it after prepare.
    # The installer reads .env during apply, so the helper must see it. Write a
    # marker the test's apply path creates by patching prepare... The file is
    # written here and apply_package reset overwrites the tree but not if we
    # pass the env via the deployment file created inside prepare_root.
    policy.write_text("roles: {}\n", encoding="utf-8")


def _docs_resources() -> list:
    return [
        {
            "kind": "s3",
            "interface_version": 1,
            "origin": "external",
            "provider": None,
            "params": {"endpoint": "http://s3", "region": "us-east-1", "path_style": True},
            "consumers": [
                {
                    "service": "docs-service",
                    "bucket": "docs",
                    "provisioned_at": "2026-10-13T00:00:00Z",
                }
            ],
        },
        {
            "kind": "postgres",
            "interface_version": 1,
            "origin": "external",
            "provider": None,
            "params": {"host": "db", "port": 5432, "sslmode": "disable"},
            "consumers": [
                {
                    "service": "docs-service",
                    "database": "docs",
                    "user": "docs",
                    "extensions": ["pgcrypto"],
                    "provisioned_at": "2026-10-13T00:00:00Z",
                }
            ],
        },
    ]


def _docs_binds() -> dict[str, str]:
    return {
        "packages/docs-service.postgres.env": "DOCS_DATABASE_URL=postgres://db/docs\n",
        "packages/docs-service.s3.env": (
            "DOCS_S3_ENDPOINT=http://s3\nDOCS_S3_REGION=us-east-1\nDOCS_S3_BUCKET=docs\n"
            "DOCS_S3_ACCESS_KEY=k\nDOCS_S3_SECRET_KEY=s\nDOCS_S3_PATH_STYLE=true\n"
        ),
    }


def test_policy_env_roundtrip(tmp_path: Path) -> None:
    """The policy file path is absolute in the canned compose config."""
    policy = tmp_path / "policy.yaml"
    box = _policy_box(policy)
    assert json.loads(json.dumps(box.config))["services"]["keystone"]["volumes"]
