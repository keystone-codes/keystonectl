"""Compose fragment goldens and structural rules."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.generate.compose import render_fragment

ROOT = Path(__file__).resolve().parent / "data"
GOLDEN = ROOT / "golden"
PACKAGES = ROOT / "packages"


def _manifest(name: str) -> dict:
    return yaml.safe_load((PACKAGES / name / "keystone-service.yaml").read_text())


@pytest.mark.parametrize("name", ["file-mcp", "docs-service", "hello-worker", "minio"])
def test_fragment_matches_golden(name: str) -> None:
    assert render_fragment(_manifest(name), {}) == (GOLDEN / f"{name}.yml").read_text()


def test_file_mcp_public_fragment_matches_golden() -> None:
    text = render_fragment(
        _manifest("file-mcp"), {}, host_port=47600, extra_hosts=["files.example.com"]
    )
    assert text == (GOLDEN / "file-mcp.public.yml").read_text()


def test_service_names_are_pkg_dash_role() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    assert "  docs-service-api:" in text
    assert "  docs-service-mcp:" in text
    assert "  docs-service-migrate:" in text


def test_role_alias_only_on_internal_network() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    assert "docs-service-internal: {aliases: [api]}" in text
    assert "docs-service-resources: {}" in text
    assert "aliases" not in text.split("docs-service-resources")[1].split("\n")[0]


def test_only_mcp_container_on_upstreams_network() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    mcp = text.split("  docs-service-mcp:\n")[1].split("\nnetworks:\n")[0]
    api = text.split("  docs-service-api:\n")[1].split("  docs-service-mcp:")[0]
    assert "docs-service-upstreams" in mcp
    assert "docs-service-upstreams" not in api


def test_every_package_gets_own_network() -> None:
    for name in ("file-mcp", "docs-service", "hello-worker", "minio"):
        text = render_fragment(_manifest(name), {})
        assert f"name: ks-pkg-{name}" in text


def test_package_network_is_plain_bridge() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    block = text.split("\n  docs-service-internal:\n")[1].split("\n  ")[0]
    assert "internal:" not in block
    assert "name: ks-pkg-docs-service" in block


def test_resource_network_only_for_needs_containers() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    api = text.split("  docs-service-api:")[1].split("  docs-service-mcp:")[0]
    migrate = text.split("  docs-service-migrate:")[1].split("  docs-service-api:")[0]
    mcp = text.split("  docs-service-mcp:")[1].split("networks:")[0]
    assert "docs-service-resources" in api
    assert "docs-service-resources" in migrate
    assert "docs-service-resources" not in mcp


def test_init_container_restart_no_and_dependants_wait_completed() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    migrate = text.split("  docs-service-migrate:")[1].split("  docs-service-api:")[0]
    assert 'restart: "no"' in migrate
    assert "healthcheck:" not in migrate
    assert "docs-service-migrate: {condition: service_completed_successfully}" in text


def test_gateway_env_file_only_on_mcp_container() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    mcp = text.split("  docs-service-mcp:")[1].split("networks:")[0]
    api = text.split("  docs-service-api:")[1].split("  docs-service-mcp:")[0]
    assert "./docs-service.mcp.env" in mcp
    assert "./docs-service.mcp.env" not in api


def test_kind_env_file_only_for_needs_containers() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    api = text.split("  docs-service-api:")[1].split("  docs-service-mcp:")[0]
    migrate = text.split("  docs-service-migrate:")[1].split("  docs-service-api:")[0]
    mcp = text.split("  docs-service-mcp:")[1]
    assert "./docs-service.postgres.env" in api
    assert "./docs-service.s3.env" in api
    assert "./docs-service.postgres.env" in migrate
    assert "./docs-service.s3.env" not in migrate
    assert "./docs-service.postgres.env" not in mcp
    assert "./docs-service.api-mcp.env" in api
    assert "./docs-service.api-mcp.env" in mcp
    assert "./docs-service.api-mcp.env" not in migrate


def test_plain_env_inline_and_dollar_escaped() -> None:
    manifest = _manifest("docs-service")
    manifest["containers"]["api"]["env"] = [{"name": "PRICE", "default": "US$5"}]
    text = render_fragment(manifest, {})
    assert "PRICE: US$$5" in text


def test_no_secret_value_in_fragment() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    assert "DOCS_INTERNAL_TOKEN" not in text
    assert "tok-" not in text


def test_no_ports_without_host_port() -> None:
    assert "ports:" not in render_fragment(_manifest("file-mcp"), {})


def test_host_port_only_on_public_container() -> None:
    text = render_fragment(_manifest("file-mcp"), {}, host_port=47600)
    assert 'ports: ["127.0.0.1:47600:8081"]' in text
    docs = render_fragment(_manifest("docs-service"), {}, host_port=47600)
    assert "ports:" not in docs


def test_extra_hosts_on_non_init_containers() -> None:
    text = render_fragment(_manifest("docs-service"), {}, extra_hosts=["docs.example.com"])
    migrate = text.split("  docs-service-migrate:")[1].split("  docs-service-api:")[0]
    api = text.split("  docs-service-api:")[1].split("  docs-service-mcp:")[0]
    assert "extra_hosts:" not in migrate
    assert "docs.example.com:host-gateway" in api


def test_fragment_never_has_dangerous_keys() -> None:
    manifest = _manifest("minio")
    manifest["containers"]["server"]["privileged"] = True
    manifest["containers"]["server"]["cap_add"] = ["SYS_ADMIN"]
    manifest["containers"]["server"]["volumes"] = [{"name": "/etc/passwd", "path": "/tmp"}]
    text = render_fragment(manifest, {})
    for key in ("privileged", "cap_add", "network_mode", "pid", "profiles"):
        assert f"{key}:" not in text
    assert "/etc/passwd" not in text


def test_newline_in_yaml_fields_cannot_open_a_key() -> None:
    manifest = _manifest("minio")
    spec = manifest["containers"].pop("server")
    spec["volumes"] = [
        {
            "name": "data",
            "path": "/data\n    - /var/run/docker.sock:/var/run/docker.sock",
        }
    ]
    spec["healthcheck"]["interval"] = "10s\n    network_mode: host\n    ports:\n      - 80:80"
    spec["healthcheck"]["timeout"] = "5s\n    privileged: true"
    spec["healthcheck"]["start_period"] = "20s\n    pid: host"
    spec["healthcheck"]["retries"] = "5\n    cap_add: [ALL]"
    role = "server\n  privileged: true\n  network_mode: host"
    manifest["containers"][role] = spec
    text = render_fragment(manifest, {})
    loaded = yaml.safe_load(text)
    for service in loaded["services"].values():
        assert "privileged" not in service
        assert "network_mode" not in service
        assert "pid" not in service
        assert "ports" not in service
        assert "cap_add" not in service
        for volume in service.get("volumes") or []:
            source = str(volume).split(":", 1)[0]
            assert "docker.sock" not in source
            assert not source.startswith("/")
    assert "\n    network_mode:" not in text
    assert "\n    privileged:" not in text
    assert "\n    - /var/run/docker.sock:" not in text


def test_fragment_keys_do_not_clash() -> None:
    texts = [render_fragment(_manifest(name), {}) for name in ("file-mcp", "docs-service", "minio")]
    networks: list[str] = []
    volumes: list[str] = []
    for text in texts:
        for line in text.splitlines():
            if line.startswith("  ") and line.endswith(":") and "internal" in line or False:
                pass
        networks.extend(
            line.strip()[:-1]
            for line in text.split("networks:")[-1].splitlines()
            if line.startswith("  ") and line.endswith(":") and not line.startswith("    ")
        )
        if "volumes:" in text:
            volumes.extend(
                line.strip()[:-1]
                for line in text.split("volumes:")[-1].splitlines()
                if line.startswith("  ") and line.endswith(":") and not line.startswith("    ")
            )
    assert len(networks) == len(set(networks))
    assert len(volumes) == len(set(volumes))


def test_named_volumes_prefixed_with_package() -> None:
    text = render_fragment(_manifest("docs-service"), {})
    assert "docs-service-cache:/var/cache/docs" in text
    assert "name: ks-pkg-docs-service-cache" in text


def test_fragment_keeps_image_user_and_default_capabilities() -> None:
    for name in ("file-mcp", "docs-service", "hello-worker", "minio"):
        doc = yaml.safe_load(render_fragment(_manifest(name), {}))
        for spec in doc["services"].values():
            assert "user" not in spec
            assert "cap_drop" not in spec
            assert spec.get("security_opt") == ["no-new-privileges:true"]

    manifest = _manifest("minio")
    manifest["containers"]["server"]["user"] = "1000:1000"
    doc = yaml.safe_load(render_fragment(manifest, {}))
    assert doc["services"]["minio-server"]["user"] == "1000:1000"
