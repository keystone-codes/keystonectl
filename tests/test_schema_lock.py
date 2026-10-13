"""Lock schema and semantic checks."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from keystonectl.layout import LOCK_SCHEMA_PATH, lock_errors

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "spec" / "examples" / "services.lock.yaml"
COMMIT = "a" * 40
SHA = "b" * 64


def _stamp() -> str:
    return "2026-10-12T10:15:00Z"


def _generated(**overrides: object) -> dict:
    service: dict = {
        "name": "file-mcp",
        "role": "service",
        "managed": "generated",
        "source": "keystone-codes",
        "package": "file-mcp",
        "version": "1.0.0",
        "ref": "v1.0.0",
        "commit": COMMIT,
        "manifest_sha256": SHA,
        "containers": {
            "mcp": {
                "service": "file-mcp-mcp",
                "image": "ghcr.io/keystone-codes/file-mcp@sha256:" + SHA,
                "init": False,
            }
        },
        "fragment": "packages/file-mcp.yml",
        "fragment_sha256": SHA,
        "registry_name": None,
        "namespace": None,
        "url": None,
        "tools": [],
        "filters": None,
        "env": {
            "keystone": [],
            "files": ["packages/file-mcp.env"],
            "generated": ["FILE_MCP_SIGNING_SECRET"],
        },
        "answers": {},
        "resources": [],
        "public": {"state": "none"},
        "installed_at": _stamp(),
        "updated_at": _stamp(),
    }
    service.update(overrides)
    return service


def _lock(services: list | None = None, resources: list | None = None) -> dict:
    return {
        "schema_version": 1,
        "layout_version": 1,
        "updated_at": _stamp(),
        "updated_by": "keystonectl 0.1.0",
        "layout": None,
        "services": [] if services is None else services,
        "resources": [] if resources is None else resources,
    }


def test_lock_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(LOCK_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        pytest.fail(str(exc))


def test_lock_example_valid() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert lock_errors(document) == []


def test_lock_rejects_unknown_field() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["services"][0]["password"] = "x"
    errors = lock_errors(document)
    assert errors
    assert any("password" in error for error in errors)


def test_lock_managed_modes() -> None:
    missing_hash = _generated()
    del missing_hash["fragment_sha256"]
    assert any("fragment_sha256" in error for error in lock_errors(_lock([missing_hash])))

    remote = {
        "name": "remote-api",
        "role": "service",
        "managed": "remote",
        "registry_name": "remote-api",
        "namespace": "remote",
        "url": "http://remote-api:8081/mcp",
        "tools": ["ping"],
        "filters": None,
        "package": "remote-api",
    }
    assert any("package" in error for error in lock_errors(_lock([remote])))

    as_resource = _generated(name="minio", role="resource", managed="remote", package="minio")
    as_resource["containers"] = {
        "server": {
            "service": "minio-server",
            "image": "minio/minio@sha256:" + SHA,
            "init": False,
        }
    }
    assert any("remote" in error for error in lock_errors(_lock([as_resource])))

    named = _generated(
        name="minio",
        role="resource",
        package="minio",
        namespace="files",
    )
    named["containers"] = {
        "server": {
            "service": "minio-server",
            "image": "minio/minio@sha256:" + SHA,
            "init": False,
        }
    }
    named["fragment"] = "packages/minio.yml"
    named["env"] = {
        "keystone": [],
        "files": ["packages/minio.env"],
        "generated": ["MINIO_ROOT_USER"],
    }
    errors = lock_errors(_lock([named]))
    assert any("namespace" in error for error in errors)


def test_lock_layout_version_must_be_1() -> None:
    document = _lock()
    document["layout_version"] = 2
    assert "layout_version: this keystonectl supports layout 1" in lock_errors(document)


def test_lock_resource_params_by_kind() -> None:
    document = _lock(
        resources=[
            {
                "kind": "postgres",
                "interface_version": 1,
                "origin": "external",
                "provider": None,
                "params": {
                    "host": "db",
                    "port": 5432,
                    "sslmode": "require",
                    "bucket": "nope",
                },
                "consumers": [],
            }
        ]
    )
    errors = lock_errors(document)
    assert any("bucket" in error for error in errors)


def test_lock_service_without_mcp() -> None:
    assert lock_errors(_lock([_generated()])) == []


def test_lock_dangling_references() -> None:
    service = _generated(resources=["s3"])
    missing_kind = lock_errors(_lock([service], []))
    assert any("unknown resource 's3'" in error for error in missing_kind)

    provider = {
        "kind": "s3",
        "interface_version": 1,
        "origin": "catalog",
        "provider": "minio",
        "params": {
            "endpoint": "http://minio:9000",
            "region": "us-east-1",
            "path_style": True,
        },
        "consumers": [],
    }
    missing_provider = lock_errors(_lock([_generated()], [provider]))
    assert any("unknown service 'minio'" in error for error in missing_provider)
