"""Fragment hash, the hand-edit guard, and override check warnings."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from keystonectl.check import run_check
from keystonectl.errors import fragment_modified_message
from keystonectl.generate.compose import fragment_digest, render_fragment
from keystonectl.output import SecretRegistry
from keystonectl.plan import Context
from keystonectl.runner import FakeCommandRunner
from keystonectl.sources import ResolvedPackage
from tests.support.deploylab import apply_package
from tests.support.resources import Lab, s3_provider

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def test_fragment_sha256_written_to_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    text = (root / "packages" / "file-mcp.yml").read_text()
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    entry = lock["services"][0]
    assert entry["fragment_sha256"] == fragment_digest(text)
    assert entry["fragment_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_apply_refuses_hand_edited_fragment(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    path = root / "packages" / "file-mcp.yml"
    path.write_text(path.read_text() + "# edited\n", encoding="utf-8")
    second, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert second.code == 3
    assert second.error["id"] == "fragment_modified"
    assert "override" in second.error["hint"]
    assert fragment_modified_message("file-mcp") == second.error["hint"]


def test_check_warns_fragment_modified(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    path = root / "packages" / "file-mcp.yml"
    path.write_text(path.read_text() + "# edited\n", encoding="utf-8")
    result = run_check(root)
    assert any(item["id"] == "fragment_modified" for item in result.warnings)


def test_check_warns_override_not_included(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    override = root / "packages" / "file-mcp.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    packages = (root / "docker-compose.packages.yml").read_text()
    (root / "docker-compose.packages.yml").write_text(
        packages.replace("packages/file-mcp.override.yml", "packages/file-mcp.yml"),
        encoding="utf-8",
    )
    result = run_check(root)
    assert any(item["id"] == "override_not_included" for item in result.warnings)


def test_check_warns_override_widens_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    from keystonectl.generate import checks as checks_mod

    payload = '{"services": {"file-mcp-mcp": {"privileged": true, "ports": ["0.0.0.0:8081:8081"]}}}'
    monkeypatch.setattr(checks_mod, "read_compose_config", lambda _root: (0, payload))
    monkeypatch.setattr(checks_mod, "read_compose_version", lambda: (0, "2.29.1\n"))
    result = run_check(root)
    warning = next(item for item in result.warnings if item["id"] == "override_widens_access")
    assert "file-mcp-mcp" in warning["message"]
    assert "privileged" in warning["message"]
    assert "ports" in warning["message"]


def _patch_compose(monkeypatch: pytest.MonkeyPatch, payload: str, *, code: int = 0) -> None:
    from keystonectl.generate import checks as checks_mod

    monkeypatch.setattr(checks_mod, "read_compose_config", lambda _root: (code, payload))
    monkeypatch.setattr(checks_mod, "read_compose_version", lambda: (0, "2.29.1\n"))


def test_loopback_port_and_named_volume_are_not_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    payload = json.dumps(
        {
            "services": {
                "file-mcp-mcp": {
                    "ports": [{"host_ip": "127.0.0.1", "published": "8081", "target": 8081}],
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "ks-pkg-file-mcp-data",
                            "target": "/data",
                        }
                    ],
                }
            }
        }
    )
    _patch_compose(monkeypatch, payload)
    result = run_check(root)
    assert not any(item["id"] == "override_widens_access" for item in result.warnings)


def test_host_access_fields_are_widening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    payload = json.dumps(
        {
            "services": {
                "file-mcp-mcp": {
                    "network_mode": "host",
                    "pid": "host",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/var/run/docker.sock",
                            "target": "/var/run/docker.sock",
                        },
                        {"type": "bind", "source": "/etc/passwd", "target": "/etc/passwd"},
                    ],
                }
            }
        }
    )
    _patch_compose(monkeypatch, payload)
    result = run_check(root)
    warning = next(item for item in result.warnings if item["id"] == "override_widens_access")
    assert "file-mcp-mcp network_mode" in warning["message"]
    assert "file-mcp-mcp pid" in warning["message"]
    assert "file-mcp-mcp volumes" in warning["message"]


def test_compose_config_without_json_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    _patch_compose(monkeypatch, "", code=1)
    result = run_check(root)
    checks = result.result["checks"]
    assert any(
        item["id"] == "override_access"
        and item["status"] == "warning"
        and "JSON" in item["message"]
        for item in checks
    )
    assert not any(item["id"] == "override_access" and item["status"] == "ok" for item in checks)


def test_reapply_of_provider_stops_on_hand_edited_fragment(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.plan import run_service

    lab = Lab(tmp_path, [s3_provider()])
    first = lab.apply("minio")
    assert first.code == 0, first.error
    path = lab.root / "packages" / "minio.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("services: {}\n# edited\n", encoding="utf-8")
    second = run_service(
        lab.root,
        "apply",
        "minio",
        runner=lab.runner,
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        deployer=ComposeDeployer(),
        clock=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )
    assert second.code == 3
    assert second.error["id"] == "fragment_modified"
    assert "override" in second.error["hint"]
    assert "eject" in second.error["hint"]
    assert path.read_text(encoding="utf-8").endswith("# edited\n")


def test_provider_deploy_is_not_called_again_over_a_hand_edit(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [s3_provider()])
    first = lab.apply("minio")
    assert first.code == 0, first.error
    lock_path = lab.root / "services.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    lock["resources"] = []
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    path = lab.root / "packages" / "minio.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("services: {}\n# edited\n", encoding="utf-8")
    before = list(lab.deployer.events)
    second = lab.apply("minio")
    assert second.code == 3
    assert second.error["id"] == "fragment_modified"
    assert path.read_text(encoding="utf-8").endswith("# edited\n")
    assert lab.deployer.events == before


def _provider_package(root: Path, text: str) -> ResolvedPackage:
    dest = root / ".keystonectl" / "packages" / "local" / "minio" / "1.0.0"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "keystone-service.yaml").write_text(text, encoding="utf-8")
    return ResolvedPackage(
        source_name="local",
        name="minio",
        version="1.0.0",
        ref="v1.0.0",
        git_url="https://example.com/minio.git",
        path=".",
        kind="resource",
        keystone_mcp=None,
        provides={"kind": "s3", "version": 1},
    )


def _deploy_context(root: Path) -> Context:
    def docker(args, **_kwargs):
        raise AssertionError(list(args))

    return Context(
        root=root,
        runner=FakeCommandRunner([(["docker"], docker)]),
        secrets=SecretRegistry(),
        sets={},
        environ={},
        interactive=False,
        lock={"services": []},
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
    )


def test_provider_deploy_rejects_unparsed_manifest(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer

    document = s3_provider()
    document["containers"]["server"]["privileged"] = True
    document["containers"]["server"]["healthcheck"]["interval"] = "10s\n    network_mode: host"
    root = tmp_path / "root"
    package = _provider_package(root, yaml.safe_dump(document))
    ctx = _deploy_context(root)
    ComposeDeployer().deploy(ctx, package, role="resource")
    assert ctx.failure is not None
    assert ctx.failure.error_id == "manifest_invalid"
    assert not (root / "packages" / "minio.yml").exists()


def test_provider_deploy_keeps_a_hand_edit(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer

    document = s3_provider()
    root = tmp_path / "root"
    package = _provider_package(root, yaml.safe_dump(document))
    fragment = render_fragment(document, {})
    path = root / "packages" / "minio.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fragment + "# edited\n", encoding="utf-8")
    ctx = _deploy_context(root)
    ctx.lock = {
        "services": [
            {
                "name": "minio",
                "managed": "generated",
                "fragment_sha256": fragment_digest(fragment),
            }
        ]
    }
    ComposeDeployer().deploy(ctx, package, role="resource")
    assert ctx.failure is not None
    assert ctx.failure.error_id == "fragment_modified"
    assert path.read_text(encoding="utf-8").endswith("# edited\n")
