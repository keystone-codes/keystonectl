"""init and init --adopt on top of the layout module."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from keystonectl.cli import main
from tests.support import FIXTURES, file_hashes, load_yaml, mode


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _production_env(root: Path) -> None:
    root.joinpath(".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n"
        "KEYSTONE_MCP_RBAC_POLICY_PATH=/etc/keystone-mcp/policy/rbac-policy.yaml\n"
        "KEYSTONE_PG_PASSWORD=pg-test\n"
        "KEYSTONE_REDIS_PASSWORD=redis-test\n",
        encoding="utf-8",
    )


def test_init_empty_dir_creates_owned_files_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "empty"
    code, document = _json(capsys, ["--json", "init", str(root)])
    assert code == 0
    assert any(item["id"] == "keystone_compose_missing" for item in document["warnings"])
    assert not (root / "docker-compose.yml").exists()
    assert not (root / ".env").exists()
    assert not (root / "policy").exists()
    assert (root / "services.lock.yaml").is_file()
    assert (root / "docker-compose.packages.yml").is_file()
    assert (root / "keystonectl" / "upstreams.yaml").is_file()
    assert load_yaml(root / "services.lock.yaml")["layout"] is None


def test_init_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "empty"
    assert main(["--json", "init", str(root)]) == 0
    capsys.readouterr()
    for path in root.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes() + b"\n# kept\n")
    before = file_hashes(root)
    code, document = _json(capsys, ["--json", "init", str(root)])
    assert code == 0
    assert document["changes"] == 0
    assert file_hashes(root) == before


def test_init_file_modes_by_layout_table(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    assert main(["init", str(root)]) == 0
    assert mode(root / "services.lock.yaml") == 0o600
    for path in (root / "keystonectl").glob("*.yaml"):
        assert mode(path) == 0o644
    assert mode(root / ".keystonectl") == 0o700
    assert mode(root / ".keystonectl" / "secrets") == 0o700
    for path in (root / "packages").glob("*.env"):
        assert mode(path) == 0o600


def test_adopt_keystone_mcp_production(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "production"
    shutil.copytree(FIXTURES / "keystone-mcp-production", root)
    _production_env(root)
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 0
    layout = document["result"]["layout"]
    assert layout["flavor"] == "keystone-mcp-example"
    assert layout["keystone_service"] == "keystone"
    assert layout["upstreams_network"]["attached_by"] == "keystonectl"
    assert document["result"]["compose_command"].split().count("-f") == 2


def test_adopt_keystone_deploy_mcp(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "deploy"
    shutil.copytree(FIXTURES / "keystone-deploy-mcp", root)
    env = (FIXTURES / "keystone-deploy-mcp" / "keystone-mcp.vars").read_text(encoding="utf-8")
    (root / ".env").write_text(env, encoding="utf-8")
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 0
    layout = document["result"]["layout"]
    assert layout["flavor"] == "keystone-deploy"
    assert layout["keystone_service"] == "keystone-mcp"
    assert layout["upstreams_network"]["attached_by"] == "operator"
    assert (root / "keystonectl" / "upstreams.yaml").is_file()


def test_adopt_operator_registry_copied_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "copy"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        "    volumes:\n"
        "      - ./upstreams.yaml:/etc/keystone-mcp/upstreams.yaml:ro\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n",
        encoding="utf-8",
    )
    (root / "upstreams.yaml").write_text(
        "schema_version: 1\n"
        "entries:\n"
        "  - name: alpha\n"
        "    url: http://alpha:8081/mcp\n"
        "    namespace: alpha\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    operator = (root / "upstreams.yaml").read_bytes()
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 0
    assert any(item["id"] == "registry_moved" for item in document["warnings"])
    assert (root / "upstreams.yaml").read_bytes() == operator
    copied = load_yaml(root / "keystonectl" / "upstreams.yaml")
    assert copied["entries"][0]["name"] == "alpha"


def test_adopt_two_keystone_services_asks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ambiguous"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  one:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        "  two:\n"
        "    image: keystone-mcp:local\n"
        "    env_file: .env\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 2
    assert document["missing"][0]["var"] == "--keystone-service"
    assert document["missing"][0]["choices"] == ["one", "two"]
    assert not (root / "services.lock.yaml").exists()


def test_adopt_no_keystone_service(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "nginx"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "services:\n  nginx:\n    image: nginx:1.27\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 3
    assert document["error"]["id"] == "keystone_service_not_found"
    assert document["retryable"] is False


def test_adopt_keeps_unmanaged_upstreams_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "kept"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        "    volumes:\n"
        "      - ./upstreams.yaml:/etc/keystone-mcp/upstreams.yaml:ro\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n",
        encoding="utf-8",
    )
    (root / "upstreams.yaml").write_text(
        "schema_version: 1\n"
        "entries:\n"
        "  - name: extra\n"
        "    url: http://extra:8081/mcp\n"
        "    namespace: extra\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 0
    assert any(item["id"] == "unmanaged_upstream" for item in document["warnings"])
    assert "extra" in (root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8")
    assert (root / "upstreams.yaml").is_file()


def test_adopt_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "production"
    shutil.copytree(FIXTURES / "keystone-mcp-production", root)
    _production_env(root)
    assert main(["--json", "init", "--adopt", str(root)]) == 0
    capsys.readouterr()
    before = file_hashes(root)
    code, document = _json(capsys, ["--json", "init", "--adopt", str(root)])
    assert code == 0
    assert document["changes"] == 0
    assert file_hashes(root) == before
