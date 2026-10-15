"""keystonectl check reads the deployment and does not use the network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from keystonectl.check import register_check
from keystonectl.cli import main
from keystonectl.errors import fragment_modified_message

SHA = "b" * 64
COMMIT = "c" * 40


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _layout() -> dict:
    return {
        "flavor": "custom",
        "project": "demo",
        "compose_files": ["docker-compose.yml", "docker-compose.packages.yml"],
        "keystone_service": "keystone",
        "profiles": [],
        "keystone_image": "ghcr.io/keystone-codes/keystone-mcp:0.5.0",
        "keystone_build": None,
        "keystone_mcp_version": None,
        "env_file": ".env",
        "policy": None,
        "upstreams_network": {
            "name": "keystone-mcp-upstreams",
            "attached_by": "keystonectl",
        },
    }


def _service(**overrides: object) -> dict:
    fragment = "packages/file-mcp.yml\n"
    digest = hashlib.sha256(fragment.encode()).hexdigest()
    service = {
        "name": "file-mcp",
        "role": "service",
        "managed": "generated",
        "package": "file-mcp",
        "version": "1.0.0",
        "ref": "v1.0.0",
        "commit": COMMIT,
        "manifest_sha256": SHA,
        "compat_keystone_mcp": ">=0.5.0,<2.0",
        "containers": {
            "mcp": {
                "service": "file-mcp-mcp",
                "image": "ghcr.io/example/file-mcp@sha256:" + SHA,
                "init": False,
            }
        },
        "fragment": "packages/file-mcp.yml",
        "fragment_sha256": digest,
        "registry_name": "file-mcp",
        "namespace": "files",
        "url": "http://file-mcp:8081/mcp",
        "tools": ["ping"],
        "env": {
            "keystone": [
                "KEYSTONE_MCP_UPSTREAM_FILES_BEARER",
                "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET",
            ],
            "files": [],
            "generated": [],
        },
    }
    service.update(overrides)
    return service


def _write(
    root: Path, lock: dict, *, registry: dict | None = None, snapshot: dict | None = None
) -> None:
    root.mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "docker-compose.packages.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "packages").mkdir()
    fragment = root / "packages" / "file-mcp.yml"
    fragment.write_text("packages/file-mcp.yml\n", encoding="utf-8")
    (root / "keystonectl").mkdir()
    entries = registry or {
        "schema_version": 1,
        "entries": [
            {
                "name": "file-mcp",
                "namespace": "files",
                "url": "http://file-mcp:8081/mcp",
                "auth": {
                    "bearer_env": "KEYSTONE_MCP_UPSTREAM_FILES_BEARER",
                    "identity_secret_env": "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET",
                },
            }
        ],
    }
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(entries, sort_keys=False), encoding="utf-8"
    )
    active = snapshot or {"active": ["files_ping"]}
    (root / "keystonectl" / "catalog-snapshot.yaml").write_text(
        yaml.safe_dump(active, sort_keys=False), encoding="utf-8"
    )
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAM_FILES_BEARER=bearer-placeholder\n"
        "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET=identity-placeholder\n",
        encoding="utf-8",
    )
    (root / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )


def _lock(service: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "layout_version": 1,
        "updated_at": "2026-10-13T00:00:00Z",
        "updated_by": "keystonectl 0.1.0",
        "layout": _layout(),
        "services": [] if service is None else [service],
        "resources": [],
    }


def test_check_clean_layout_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert document["error"] is None
    assert any(item["status"] == "ok" for item in document["result"]["checks"])


def test_docker_checks_warn_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def extra(_context: object) -> dict[str, str]:
        return {"id": "containers", "status": "ok", "message": "up", "hint": ""}

    extra.needs_docker = True  # type: ignore[attr-defined]
    extra.check_id = "containers"  # type: ignore[attr-defined]
    register_check(extra)
    monkeypatch.setattr("keystonectl.check.docker_available", lambda: False)
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0
    assert any(
        item["id"] == "containers" and item["status"] == "warning"
        for item in document["result"]["checks"]
    )


def test_missing_layout_file_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert document["error"]["id"] == "check_failed"


def test_lock_schema_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    (root / "services.lock.yaml").write_text(
        "schema_version: 9\nlayout_version: 1\n", encoding="utf-8"
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert document["error"]["id"] == "check_failed"


def test_registry_entry_missing_or_different(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    service = _service()
    _write(root, _lock(service), registry={"schema_version": 1, "entries": []})
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any(
        "missing" in item["message"]
        for item in document["result"]["checks"]
        if item["id"] == "registry_entry"
    )

    for field, value in (
        ("namespace", "other"),
        ("url", "http://other:8081/mcp"),
    ):
        registry = {
            "schema_version": 1,
            "entries": [
                {
                    "name": "file-mcp",
                    "namespace": "files",
                    "url": "http://file-mcp:8081/mcp",
                    "auth": {
                        "bearer_env": "KEYSTONE_MCP_UPSTREAM_FILES_BEARER",
                        "identity_secret_env": "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET",
                    },
                }
            ],
        }
        registry["entries"][0][field] = value
        (root / "keystonectl" / "upstreams.yaml").write_text(
            yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
        )
        code, document = _json(capsys, ["--json", "--root", str(root), "check"])
        assert code == 3
        assert any(field in item["message"] for item in document["result"]["checks"])

    registry = {
        "schema_version": 1,
        "entries": [
            {
                "name": "file-mcp",
                "namespace": "files",
                "url": "http://file-mcp:8081/mcp",
                "auth": {
                    "bearer_env": "OTHER_BEARER",
                    "identity_secret_env": "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET",
                },
            }
        ],
    }
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert any("bearer_env" in item["message"] for item in document["result"]["checks"])
    registry["entries"][0]["auth"]["bearer_env"] = "KEYSTONE_MCP_UPSTREAM_FILES_BEARER"
    registry["entries"][0]["auth"]["identity_secret_env"] = "OTHER_IDENTITY"
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert any("identity_secret_env" in item["message"] for item in document["result"]["checks"])


def test_unmanaged_upstream_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    registry = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    registry["entries"].append(
        {"name": "extra", "namespace": "extra", "url": "http://extra:8081/mcp"}
    )
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0
    assert any(item["id"] == "unmanaged_upstream" for item in document["warnings"])


def test_namespace_pair_collision_across_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    _write(
        root,
        _lock(None),
        registry={
            "schema_version": 1,
            "entries": [
                {"name": "a", "namespace": "files", "url": "http://a:8081/mcp"},
                {"name": "b", "namespace": "files_more", "url": "http://b:8081/mcp"},
            ],
        },
        snapshot={"active": []},
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any(item["id"] == "namespace" for item in document["result"]["checks"])


def test_snapshot_missing_tool_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()), snapshot={"active": []})
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any("files_ping" in item["message"] for item in document["result"]["checks"])


def test_fragment_modified_warning_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    (root / "packages" / "file-mcp.yml").write_text("edited\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0
    warning = next(item for item in document["warnings"] if item["id"] == "fragment_modified")
    assert warning["message"] == fragment_modified_message("file-mcp")


def test_ejected_fragment_not_hashed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    service = _service(managed="ejected", fragment_sha256=None)
    _write(root, _lock(service))
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert any("ejected" in item["message"] for item in document["result"]["checks"])


def test_remote_service_needs_no_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    service = {
        "name": "remote-mcp",
        "role": "service",
        "managed": "remote",
        "registry_name": "remote-mcp",
        "namespace": "remote",
        "url": "http://remote:8081/mcp",
        "tools": ["ping"],
        "env": {
            "keystone": [
                "KEYSTONE_MCP_UPSTREAM_REMOTE_BEARER",
                "KEYSTONE_MCP_UPSTREAM_REMOTE_IDENTITY_SECRET",
            ],
            "files": [],
            "generated": [],
        },
    }
    registry = {
        "schema_version": 1,
        "entries": [
            {
                "name": "remote-mcp",
                "namespace": "remote",
                "url": "http://remote:8081/mcp",
                "auth": {
                    "bearer_env": "KEYSTONE_MCP_UPSTREAM_REMOTE_BEARER",
                    "identity_secret_env": "KEYSTONE_MCP_UPSTREAM_REMOTE_IDENTITY_SECRET",
                },
            }
        ],
    }
    _write(root, _lock(service), registry=registry, snapshot={"active": ["remote_ping"]})
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAM_REMOTE_BEARER=bearer-placeholder\n"
        "KEYSTONE_MCP_UPSTREAM_REMOTE_IDENTITY_SECRET=identity-placeholder\n",
        encoding="utf-8",
    )
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert not any(item["id"] == "fragment_modified" for item in document["warnings"])


def test_orphan_fragment_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    (root / "packages" / "orphan.yml").write_text("orphan\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0
    assert any(item["id"] == "orphan_fragment" for item in document["warnings"])


def test_env_name_missing_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    (root / ".env").write_text("", encoding="utf-8")
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any(item["id"] == "env_name" for item in document["result"]["checks"])


def test_env_files_mode_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    service = _service()
    service["env"]["files"] = ["packages/file-mcp.env"]
    _write(root, _lock(service))
    env = root / "packages" / "file-mcp.env"
    env.write_text("TOKEN=value\n", encoding="utf-8")
    env.chmod(0o644)
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any(item["id"] == "secret_file_unsafe" for item in document["result"]["checks"])


def test_register_check_extension_point(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def extra(_context: object) -> dict[str, str]:
        return {"id": "sample", "status": "ok", "message": "sample ran", "hint": ""}

    register_check(extra)
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0
    assert any(item["id"] == "sample" for item in document["result"]["checks"])


def test_incompatible_keystone_mcp_per_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service(compat_keystone_mcp=">=0.9.0,<2.0")))
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 3
    assert any(item["id"] == "incompatible_keystone_mcp" for item in document["result"]["checks"])


def test_check_never_prints_secret_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    secret = "distinct-secret-value"
    (root / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAM_FILES_BEARER=" + secret + "\n"
        "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET=identity-placeholder\n",
        encoding="utf-8",
    )
    code = main(["--json", "--root", str(root), "check"])
    text = capsys.readouterr().out
    assert code == 0
    assert secret not in text


def _older_tag(ref: str) -> str:
    from packaging.version import Version

    version = Version(ref.removeprefix("v"))
    if version.micro > 0:
        return f"v{version.major}.{version.minor}.{version.micro - 1}"
    if version.minor > 0:
        return f"v{version.major}.{version.minor - 1}.0"
    return f"v{version.major - 1}.9.0"


def _pin_default_source(root: Path, ref: str) -> None:
    from keystonectl.catalog import DEFAULT_SOURCES

    document = {
        "schema_version": 1,
        "sources": [
            {
                "name": "keystone-codes",
                "url": DEFAULT_SOURCES[0].url,
                "ref": ref,
                "priority": 100,
            }
        ],
    }
    (root / "sources.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_default_catalog_behind_on_check_until_set_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from keystonectl.catalog import DEFAULT_CATALOG_REF
    from keystonectl.sources import set_source_ref
    from tests.support.served_catalog import SHA as git_sha
    from tests.support.served_catalog import catalog_runner

    root = tmp_path / "root"
    _write(root, _lock(_service()))
    behind = _older_tag(DEFAULT_CATALOG_REF)
    _pin_default_source(root, behind)
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert any(item["id"] == "default_catalog_behind" for item in document["warnings"])
    tags = f"{git_sha}\trefs/tags/{behind}\n{git_sha}\trefs/tags/{DEFAULT_CATALOG_REF}\n"
    moved = set_source_ref(root, "keystone-codes", DEFAULT_CATALOG_REF, catalog_runner(tags=tags))
    assert moved.code == 0, moved.error
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert all(item["id"] != "default_catalog_behind" for item in document["warnings"])


def test_check_sha_pin_has_no_default_catalog_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    _write(root, _lock(_service()))
    _pin_default_source(root, "a" * 40)
    code, document = _json(capsys, ["--json", "--root", str(root), "check"])
    assert code == 0, document
    assert all(item["id"] != "default_catalog_behind" for item in document["warnings"])
