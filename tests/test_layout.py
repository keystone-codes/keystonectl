"""Init, adopt, packages compose, and the managed env block."""

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from keystonectl.layout import (
    Deployment,
    LayoutError,
    apply_init,
    lock_errors,
    plan_init,
    render_packages_compose,
    replace_managed_env_block,
)

NOW = datetime(2026, 10, 12, 10, 15, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layouts"
START = "# >>> keystonectl: managed block, do not edit >>>"
END = "# <<< keystonectl <<<"


def _apply(
    root: Path, *, adopt: bool, environ: dict[str, str] | None = None, service: str | None = None
):
    plan = plan_init(root, adopt=adopt, keystone_service=service, environ=environ or {})
    written = apply_init(root, plan, now=NOW)
    return plan, written


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtimes(root: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.stat().st_mtime_ns
    return found


def _copy_fixture(name: str, dest: Path) -> None:
    shutil.copytree(FIXTURES / name, dest)


def _production_env(root: Path) -> None:
    root.joinpath(".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n"
        "KEYSTONE_MCP_RBAC_POLICY_PATH=/etc/keystone-mcp/policy/rbac-policy.yaml\n"
        "KEYSTONE_PG_PASSWORD=pg-test\n"
        "KEYSTONE_REDIS_PASSWORD=redis-test\n",
        encoding="utf-8",
    )


def _write_compose(root: Path, text: str, env: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text(text, encoding="utf-8")
    (root / ".env").write_text(
        env if env.endswith("\n") or env == "" else env + "\n", encoding="utf-8"
    )


def _keystone_compose(extra: str = "") -> str:
    return (
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        f"{extra}"
    )


def test_plan_init_does_not_write(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    plan = plan_init(root, adopt=False, environ={})
    assert not root.exists()
    assert plan.ready is False
    assert plan.changes > 0


def test_init_empty_dir_creates_owned_files(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    plan, written = _apply(root, adopt=False)
    assert plan.ready is False
    assert any(item.startswith("keystone_compose_missing") for item in plan.warnings)
    assert written
    assert _mode(root / "packages") == 0o755
    assert _mode(root / "keystonectl") == 0o755
    assert _mode(root / ".keystonectl") == 0o700
    assert _mode(root / ".keystonectl" / "secrets") == 0o700
    assert _mode(root / ".keystonectl" / "tmp") == 0o700
    assert _mode(root / "services.lock.yaml") == 0o600
    assert _mode(root / "keystonectl" / "upstreams.yaml") == 0o644
    assert _mode(root / "keystonectl" / "catalog-snapshot.yaml") == 0o644
    assert (root / "docker-compose.packages.yml").is_file()
    assert (root / ".keystonectl" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (root / ".gitignore").read_text(encoding="utf-8") == (
        "/services.lock.yaml*\n/packages/*.env\n/.keystonectl/\n/.env\n/sources.yaml\n"
    )
    assert not (root / "sources.yaml").exists()
    lock = yaml.safe_load((root / "services.lock.yaml").read_text(encoding="utf-8"))
    assert lock["layout"] is None
    assert lock["layout_version"] == 1
    assert lock_errors(lock) == []
    registry = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    assert registry == {"schema_version": 1, "entries": []}
    snapshot = yaml.safe_load(
        (root / "keystonectl" / "catalog-snapshot.yaml").read_text(encoding="utf-8")
    )
    assert snapshot == {"active": []}


def test_init_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    _apply(root, adopt=False)
    before = _mtimes(root)
    contents = {name: (root / name).read_bytes() for name in before}
    plan, written = _apply(root, adopt=False)
    assert plan.changes == 0
    assert written == []
    assert _mtimes(root) == before
    assert {name: (root / name).read_bytes() for name in before} == contents


def test_init_never_overwrites_existing_files(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    _apply(root, adopt=False)
    custom = (
        "schema_version: 1\n"
        "entries:\n"
        "  - name: kept\n"
        "    url: http://kept:8081/mcp\n"
        "    namespace: kept\n"
        "    enabled: true\n"
    )
    target = root / "keystonectl" / "upstreams.yaml"
    target.write_text(custom, encoding="utf-8")
    stamp = target.stat().st_mtime_ns
    plan, written = _apply(root, adopt=False)
    assert "keystonectl/upstreams.yaml" not in written
    assert plan.changes == 0
    assert target.read_text(encoding="utf-8") == custom
    assert target.stat().st_mtime_ns == stamp


def test_init_foreign_dir_refused(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=False, environ={})
    assert caught.value.code == "root_not_empty"
    assert caught.value.exit_code == 3


def test_init_with_compose_requires_adopt(tmp_path: Path) -> None:
    root = tmp_path / "compose"
    _write_compose(root, "services: {}\n")
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=False, environ={})
    assert caught.value.code == "adopt_required"
    assert "use --adopt" in caught.value.message


def test_adopt_keystone_mcp_production(tmp_path: Path) -> None:
    root = tmp_path / "production"
    _copy_fixture("keystone-mcp-production", root)
    _production_env(root)
    operator = (root / "upstreams.yaml").read_bytes()
    plan, _written = _apply(root, adopt=True)
    assert plan.ready is True
    assert plan.deployment is not None
    deployment = plan.deployment
    # The published example pins 0.5.0; the card text still said 0.4.0.
    assert deployment.keystone_service == "keystone"
    assert deployment.keystone_image == "ghcr.io/keystone-codes/keystone-mcp:0.5.0"
    assert deployment.flavor == "keystone-mcp-example"
    assert deployment.project == "keystone-production"
    assert deployment.policy == {
        "file": "policy/rbac-policy.yaml",
        "container_path": "/etc/keystone-mcp/policy/rbac-policy.yaml",
    }
    assert deployment.upstreams_network["name"] == "keystone-mcp-upstreams"
    assert deployment.upstreams_network["attached_by"] == "keystonectl"
    assert not deployment.upstreams_network.get("include_default_network")
    assert (root / "upstreams.yaml").read_bytes() == operator
    registry = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    assert registry["entries"] == []
    assert not any(item.startswith("registry_moved") for item in plan.warnings)
    packages = (root / "docker-compose.packages.yml").read_text(encoding="utf-8")
    assert "include:" not in packages
    assert "services:\n  keystone:\n" in packages
    assert "KEYSTONE_MCP_UPSTREAMS_PATH: /etc/keystone-mcp/keystonectl/upstreams.yaml" in packages
    assert (
        "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT: /etc/keystone-mcp/keystonectl/catalog-snapshot.yaml"
        in packages
    )
    assert "./keystonectl:/etc/keystone-mcp/keystonectl:ro" in packages
    assert "networks: [keystone-mcp-upstreams]" in packages
    assert "default" not in packages
    assert plan.compose_command == [
        "docker",
        "compose",
        "--project-directory",
        os.fspath(root),
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.packages.yml",
        "up",
        "-d",
    ]
    again, written = _apply(root, adopt=True)
    assert again.changes == 0
    assert written == []


def test_adopt_keystone_deploy_mcp(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    _copy_fixture("keystone-deploy-mcp", root)
    env = (FIXTURES / "keystone-deploy-mcp" / "keystone-mcp.vars").read_text(encoding="utf-8")
    (root / ".env").write_text(env, encoding="utf-8")
    plan, _written = _apply(root, adopt=True)
    assert plan.deployment is not None
    deployment = plan.deployment
    assert deployment.keystone_service == "keystone-mcp"
    assert deployment.profiles == ("keystone-mcp",)
    assert deployment.keystone_build == "./keystone-mcp"
    assert deployment.keystone_image == "keystone-mcp:local"
    assert deployment.flavor == "keystone-deploy"
    assert deployment.upstreams_network["attached_by"] == "operator"
    packages = (root / "docker-compose.packages.yml").read_text(encoding="utf-8")
    assert "networks:" not in packages
    assert any(item.startswith("inline_registry_ignored") for item in plan.warnings)
    assert any(item.startswith("policy_not_configured") for item in plan.warnings)
    assert "--profile" in plan.compose_command
    assert "keystone-mcp" in plan.compose_command
    assert plan.compose_command.count("-f") == 2


def test_adopt_copies_operator_registry_once(tmp_path: Path) -> None:
    root = tmp_path / "copy"
    _write_compose(
        root,
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        "    networks: [keystone]\n"
        "    volumes:\n"
        "      - ./upstreams.yaml:/etc/keystone-mcp/upstreams.yaml:ro\n"
        "      - ./catalog-snapshot.yaml:/etc/keystone-mcp/catalog-snapshot.yaml:ro\n"
        "networks:\n"
        "  keystone: {}\n",
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n"
        "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT=/etc/keystone-mcp/catalog-snapshot.yaml\n",
    )
    operator_registry = (
        "schema_version: 1\n"
        "entries:\n"
        "  - name: alpha\n"
        "    url: http://alpha:8081/mcp\n"
        "    namespace: alpha\n"
        "    enabled: true\n"
        "  - name: beta\n"
        "    url: http://beta:8081/mcp\n"
        "    namespace: beta\n"
        "    enabled: true\n"
    )
    operator_snapshot = "active:\n  - alpha_ping\nreserved:\n  - alpha_drop\nwip:\n  - alpha_wip\n"
    (root / "upstreams.yaml").write_text(operator_registry, encoding="utf-8")
    (root / "catalog-snapshot.yaml").write_text(operator_snapshot, encoding="utf-8")
    plan, _written = _apply(root, adopt=True)
    assert any(item.startswith("registry_moved") for item in plan.warnings)
    assert any(item.startswith("snapshot_moved") for item in plan.warnings)
    assert (root / "upstreams.yaml").read_text(encoding="utf-8") == operator_registry
    copied = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    assert [item["name"] for item in copied["entries"]] == ["alpha", "beta"]
    snapshot = yaml.safe_load(
        (root / "keystonectl" / "catalog-snapshot.yaml").read_text(encoding="utf-8")
    )
    assert snapshot["active"] == ["alpha_ping"]
    assert snapshot["reserved"] == ["alpha_drop"]
    assert snapshot["wip"] == ["alpha_wip"]
    registry_bytes = (root / "keystonectl" / "upstreams.yaml").read_bytes()
    snapshot_bytes = (root / "keystonectl" / "catalog-snapshot.yaml").read_bytes()
    (root / "upstreams.yaml").write_text(
        operator_registry + "  - name: gamma\n    url: http://gamma:8081/mcp\n",
        encoding="utf-8",
    )
    (root / "catalog-snapshot.yaml").write_text(
        "active:\n  - alpha_ping\n  - extra_tool\n",
        encoding="utf-8",
    )
    again, written = _apply(root, adopt=True)
    assert "keystonectl/upstreams.yaml" not in written
    assert "keystonectl/catalog-snapshot.yaml" not in written
    assert again.changes == 0
    assert (root / "keystonectl" / "upstreams.yaml").read_bytes() == registry_bytes
    assert (root / "keystonectl" / "catalog-snapshot.yaml").read_bytes() == snapshot_bytes
    assert not any(item.startswith("registry_moved") for item in again.warnings)
    assert not any(item.startswith("snapshot_moved") for item in again.warnings)


def test_adopt_keystone_deploy_inline_registry_refused(tmp_path: Path) -> None:
    root = tmp_path / "inline"
    _copy_fixture("keystone-deploy-mcp", root)
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAMS="
        '[{"name":"a","url":"http://inline-marker:8081/mcp","namespace":"a","enabled":true}]\n',
        encoding="utf-8",
    )
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "registry_inline"
    assert caught.value.exit_code == 3
    assert "inline-marker" not in caught.value.message
    assert not (root / "services.lock.yaml").exists()


def test_adopt_foreign_compose_refused(tmp_path: Path) -> None:
    root = tmp_path / "nginx"
    _write_compose(root, "services:\n  nginx:\n    image: nginx:1.27\n")
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "keystone_service_not_found"
    assert "--keystone-service" in caught.value.message


def test_adopt_empty_dir_refused(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "compose_not_found"


def test_adopt_ambiguous_keystone_service(tmp_path: Path) -> None:
    root = tmp_path / "ambiguous"
    _write_compose(
        root,
        "services:\n"
        "  one:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        "  two:\n"
        "    image: keystone-mcp:local\n"
        "    env_file: .env\n",
    )
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "keystone_service_ambiguous"
    assert caught.value.exit_code == 2
    assert caught.value.choices == ["one", "two"]
    plan, _written = _apply(root, adopt=True, service="two")
    assert plan.deployment is not None
    assert plan.deployment.keystone_service == "two"


def test_adopt_path_outside_root_refused(tmp_path: Path) -> None:
    root = tmp_path / "outside"
    _write_compose(
        root,
        _keystone_compose(
            "    volumes:\n      - /etc/keystone-outside.yaml:/etc/keystone-mcp/upstreams.yaml:ro\n"
        ),
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n",
    )
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "path_outside_root"


def test_adopt_registry_path_unmapped_refused(tmp_path: Path) -> None:
    root = tmp_path / "unmapped"
    _write_compose(
        root,
        _keystone_compose("    volumes:\n      - ./policy:/etc/keystone-mcp/policy\n"),
        "KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml\n",
    )
    with pytest.raises(LayoutError) as caught:
        plan_init(root, adopt=True, environ={})
    assert caught.value.code == "registry_path_unmapped"


def test_operator_files_untouched(tmp_path: Path) -> None:
    root = tmp_path / "production"
    _copy_fixture("keystone-mcp-production", root)
    _production_env(root)
    paths = [
        root / "docker-compose.yml",
        root / ".env",
        root / "policy" / "rbac-policy.yaml",
        root / "upstreams.yaml",
    ]
    before = {path: _sha(path) for path in paths}
    plan = plan_init(root, adopt=True, environ={})
    assert {path: _sha(path) for path in paths} == before
    apply_init(root, plan, now=NOW)
    assert {path: _sha(path) for path in paths} == before


def test_compose_interpolation(tmp_path: Path) -> None:
    text = (
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:${KEYSTONE_MCP_TAG:-0.5.0}\n"
        "    env_file: .env\n"
    )
    with_tag = tmp_path / "with-tag"
    _write_compose(with_tag, text, "KEYSTONE_MCP_TAG=9.9.9\n")
    plan, _written = _apply(with_tag, adopt=True)
    assert plan.deployment is not None
    assert plan.deployment.keystone_image == "ghcr.io/keystone-codes/keystone-mcp:9.9.9"

    defaulted = tmp_path / "defaulted"
    _write_compose(defaulted, text, "")
    plan, _written = _apply(defaulted, adopt=True)
    assert plan.deployment is not None
    assert plan.deployment.keystone_image == "ghcr.io/keystone-codes/keystone-mcp:0.5.0"
    assert not any(item.startswith("compose_variable_unset") for item in plan.warnings)

    overridden = tmp_path / "overridden"
    _write_compose(overridden, text, "KEYSTONE_MCP_TAG=9.9.9\n")
    plan, _written = _apply(overridden, adopt=True, environ={"KEYSTONE_MCP_TAG": "1.2.3"})
    assert plan.deployment is not None
    assert plan.deployment.keystone_image == "ghcr.io/keystone-codes/keystone-mcp:1.2.3"

    missing = tmp_path / "missing"
    _write_compose(
        missing,
        "name: demo\n"
        "services:\n"
        "  keystone:\n"
        "    image: ghcr.io/keystone-codes/keystone-mcp:0.5.0\n"
        "    env_file: .env\n"
        '    command: "echo ${MISSING_TAG}"\n',
    )
    plan, _written = _apply(missing, adopt=True)
    assert plan.deployment is not None
    assert any("MISSING_TAG" in item for item in plan.warnings)


def test_service_without_networks_gets_default(tmp_path: Path) -> None:
    root = tmp_path / "default-net"
    _write_compose(root, _keystone_compose())
    plan, _written = _apply(root, adopt=True)
    packages = (root / "docker-compose.packages.yml").read_text(encoding="utf-8")
    assert "networks: [default, keystone-mcp-upstreams]" in packages
    assert plan.deployment is not None
    assert plan.deployment.upstreams_network["include_default_network"] is True


def test_render_packages_compose_is_deterministic() -> None:
    deployment = Deployment(
        flavor="custom",
        project="demo",
        compose_files=("docker-compose.yml", "docker-compose.packages.yml"),
        keystone_service="keystone",
        profiles=(),
        keystone_image="ghcr.io/keystone-codes/keystone-mcp:0.5.0",
        keystone_build=None,
        keystone_mcp_version=None,
        env_file=".env",
        policy=None,
        upstreams_network={"name": "keystone-mcp-upstreams", "attached_by": "keystonectl"},
    )
    first = render_packages_compose(deployment)
    assert first == render_packages_compose(deployment)
    assert "include:" not in first
    assert "KEYSTONE_MCP_UPSTREAMS_PATH:" in first
    assert "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT:" in first
    assert "./keystonectl:/etc/keystone-mcp/keystonectl:ro" in first
    with_packages = render_packages_compose(deployment, ["zeta", "alpha", "alpha"])
    assert with_packages == render_packages_compose(deployment, ["alpha", "zeta"])
    assert with_packages.index("packages/alpha.yml") < with_packages.index("packages/zeta.yml")


def test_init_updates_lock_when_layout_changes(tmp_path: Path) -> None:
    root = tmp_path / "later"
    _apply(root, adopt=False)
    lock_path = root / "services.lock.yaml"
    document = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    document["services"] = [{"name": "kept"}]
    lock_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    _write_compose(root, _keystone_compose())
    plan, _written = _apply(root, adopt=False)
    assert any(item.startswith("layout_changed:") for item in plan.warnings)
    assert "keystone_service" in plan.warnings[-1] or any(
        "keystone_service" in item for item in plan.warnings
    )
    updated = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert updated["services"] == [{"name": "kept"}]
    assert updated["layout"]["keystone_service"] == "keystone"


def test_init_preserves_answered_keystone_mcp_version(tmp_path: Path) -> None:
    root = tmp_path / "version"
    _write_compose(root, _keystone_compose())
    # A compose file without a lock must be adopted first.
    _apply(root, adopt=True)
    lock_path = root / "services.lock.yaml"
    document = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    document["layout"]["keystone_mcp_version"] = "0.5.0"
    lock_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    plan, written = _apply(root, adopt=False)
    assert plan.changes == 0
    assert written == []
    updated = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert updated["layout"]["keystone_mcp_version"] == "0.5.0"


@pytest.mark.parametrize(
    "case",
    ["append", "replace", "preserve", "conflict", "prefix"],
)
def test_managed_env_block(case: str) -> None:
    if case == "append":
        result = replace_managed_env_block(
            "FOO=bar\n",
            {"KEYSTONE_MCP_UPSTREAM_FILES_BEARER": "value-one"},
        )
        assert result.startswith("FOO=bar\n")
        assert result.endswith(f"{START}\nKEYSTONE_MCP_UPSTREAM_FILES_BEARER=value-one\n{END}\n")
        return
    if case == "replace":
        original = (
            f"FOO=bar\n{START}\nKEYSTONE_MCP_UPSTREAM_FILES_BEARER=old-value\n{END}\nAFTER=1\n"
        )
        result = replace_managed_env_block(
            original,
            {"KEYSTONE_MCP_UPSTREAM_FILES_BEARER": "value-two"},
        )
        assert result == (
            f"FOO=bar\n{START}\nKEYSTONE_MCP_UPSTREAM_FILES_BEARER=value-two\n{END}\nAFTER=1\n"
        )
        assert "old-value" not in result
        return
    if case == "preserve":
        original = "# KEYSTONE_MCP_UPSTREAM_FILES_BEARER=mentioned\nOTHER=keep\n\n"
        result = replace_managed_env_block(
            original,
            {"KEYSTONE_MCP_UPSTREAM_FILES_BEARER": "value-one"},
        )
        assert result.startswith(original)
        assert "OTHER=keep" in result
        return
    if case == "conflict":
        with pytest.raises(LayoutError) as caught:
            replace_managed_env_block(
                "KEYSTONE_MCP_UPSTREAM_FILES_BEARER=already\n",
                {"KEYSTONE_MCP_UPSTREAM_FILES_BEARER": "value-secret"},
            )
        assert caught.value.code == "env_name_conflict"
        assert "value-secret" not in caught.value.message
        return
    with pytest.raises(ValueError):
        replace_managed_env_block("", {"FILE_MCP_SIGNING_SECRET": "nope"})
