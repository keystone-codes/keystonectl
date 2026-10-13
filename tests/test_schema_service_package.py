"""Validation of keystone-service.yaml schema_version 1."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema.validators import Draft202012Validator

from keystonectl.keystone_mcp_rules import BUILTIN_TOOL_NAMES
from keystonectl.manifest import (
    FORBIDDEN_HOST_KEYS,
    INSTALLER_KEYS,
    manifest_errors,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "keystonectl"
    / "schemas"
    / "service-package-v1.json"
)
DIGEST = "0123456789abcdef" * 4
NAME_PATTERN = r"^(?!.*--)[a-z][a-z0-9-]{1,38}[a-z0-9]$"
VERSION_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
POLICY_PATTERN = r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,128}\.ya?ml$"

_S3_BIND = {
    "endpoint": "FILE_MCP_S3_ENDPOINT",
    "region": "FILE_MCP_S3_REGION",
    "bucket": "FILE_MCP_S3_BUCKET",
    "access_key": "FILE_MCP_S3_ACCESS_KEY",
    "secret_key": "FILE_MCP_S3_SECRET_KEY",
    "path_style": "FILE_MCP_S3_PATH_STYLE",
}
_POSTGRES_PARTS = {
    "host": "DOCS_DB_HOST",
    "port": "DOCS_DB_PORT",
    "database": "DOCS_DB_NAME",
    "user": "DOCS_DB_USER",
    "password": "DOCS_DB_PASSWORD",
    "sslmode": "DOCS_DB_SSLMODE",
}


def _image(name: str, tag: str | None = "1.0.0") -> str:
    tag_part = f":{tag}" if tag else ""
    return f"{name}{tag_part}@sha256:{DIGEST}"


def _live(name: str, port: int, **extra: object) -> dict:
    container: dict = {
        "image": _image(name),
        "port": port,
        "healthcheck": {"path": "/healthz"},
    }
    container.update(extra)
    return container


def _service() -> dict:
    return {
        "schema_version": 1,
        "kind": "service",
        "name": "file-mcp",
        "version": "1.0.0",
        "publisher": "keystone-codes",
        "summary": "Upload and download links for large files on S3-compatible storage.",
        "homepage": "https://github.com/keystone-codes/file-mcp",
        "compat": {
            "keystone_mcp": ">=0.5.0,<2",
            "contracts": ["upstream-registry/1", "identity-assertion/1"],
        },
        "mcp": {
            "container": "mcp",
            "namespace": "files",
            "tools": ["create_upload_url", "get_download_link", "get_file_info"],
            "critical": ["create_upload_url", "get_download_link"],
            "bearer_env": "FILE_MCP_BEARER_TOKEN",
        },
        "identity": {
            "signed_assertion": "required",
            "secret_env": "FILE_MCP_IDENTITY_SECRET",
            "audience_env": "FILE_MCP_REGISTRY_NAME",
        },
        "needs": [{"kind": "s3", "version": 1, "bind_env": dict(_S3_BIND)}],
        "public_paths": ["/u/", "/d/"],
        "public_base_url_env": "FILE_MCP_PUBLIC_BASE_URL",
        "recommended_policy": "policy.example.yaml",
        "containers": {
            "mcp": _live(
                "ghcr.io/keystone-codes/file-mcp",
                8081,
                env=[
                    {"name": "FILE_MCP_MAX_BYTES"},
                    {"name": "FILE_MCP_LINK_TTL_SECONDS"},
                ],
                secrets=[{"name": "FILE_MCP_SIGNING_SECRET", "generate": {"bytes": 48}}],
            )
        },
    }


def _multi() -> dict:
    shared_secret = {"name": "DOCS_INTERNAL_TOKEN", "generate": {"bytes": 32}}
    return {
        "schema_version": 1,
        "kind": "service",
        "name": "docs-service",
        "version": "1.0.0",
        "publisher": "keystone-codes",
        "summary": "Document search behind an MCP facade, with its own API.",
        "compat": {
            "keystone_mcp": ">=0.5.0,<2",
            "contracts": ["upstream-registry/1", "identity-assertion/1"],
        },
        "mcp": {
            "container": "mcp",
            "namespace": "docs",
            "tools": ["search_docs", "read_page"],
            "bearer_env": "DOCS_MCP_BEARER",
        },
        "identity": {
            "signed_assertion": "required",
            "secret_env": "DOCS_IDENTITY_SECRET",
            "audience_env": "DOCS_REGISTRY_NAME",
        },
        "needs": [
            {
                "kind": "postgres",
                "version": 1,
                "bind_env": {"url": "DOCS_DATABASE_URL"},
                "extensions": ["pgcrypto"],
                "containers": ["migrate", "api"],
            },
            {
                "kind": "s3",
                "version": 1,
                "bind_env": {
                    "endpoint": "DOCS_S3_ENDPOINT",
                    "region": "DOCS_S3_REGION",
                    "bucket": "DOCS_S3_BUCKET",
                    "access_key": "DOCS_S3_ACCESS_KEY",
                    "secret_key": "DOCS_S3_SECRET_KEY",
                    "path_style": "DOCS_S3_PATH_STYLE",
                },
                "containers": ["api"],
            },
        ],
        "containers": {
            "migrate": {
                "image": _image("ghcr.io/example/docs-service"),
                "init": True,
                "command": ["docs-service", "migrate"],
            },
            "api": _live(
                "ghcr.io/example/docs-service",
                8000,
                volumes=[{"name": "cache", "path": "/var/cache/docs"}],
                secrets=[deepcopy(shared_secret)],
            ),
            "mcp": _live(
                "ghcr.io/example/docs-service",
                8081,
                env=[{"name": "DOCS_API_URL", "default": "http://api:8000"}],
                secrets=[deepcopy(shared_secret)],
            ),
        },
    }


def _resource() -> dict:
    return {
        "schema_version": 1,
        "kind": "resource",
        "name": "minio",
        "version": "1.0.0",
        "publisher": "keystone-codes",
        "summary": "S3-compatible object storage for packages that need s3/v1.",
        "containers": {
            "server": {
                "image": _image("minio/minio", tag=None),
                "port": 9000,
                "command": ["server", "/data", "--quiet"],
                "env": [{"name": "MINIO_BROWSER", "default": "off"}],
                "volumes": [{"name": "data", "path": "/data"}],
                "secrets": [
                    {"name": "MINIO_ROOT_USER", "generate": {"bytes": 16}},
                    {"name": "MINIO_ROOT_PASSWORD", "generate": {"bytes": 32}},
                ],
                "healthcheck": {"command": ["mc", "ready", "local"]},
            }
        },
        "provides": {"kind": "s3", "version": 1, "container": "server"},
        "provision": {
            "per_consumer": "bucket_and_user",
            "image": _image("minio/mc", tag=None),
            "script": "provision.sh",
            "admin_env": {"user": "MINIO_ROOT_USER", "password": "MINIO_ROOT_PASSWORD"},
        },
    }


def _postgres() -> dict:
    return {
        "schema_version": 1,
        "kind": "resource",
        "name": "postgres",
        "version": "1.0.0",
        "publisher": "keystone-codes",
        "summary": "PostgreSQL for packages that need postgres/v1.",
        "containers": {
            "db": {
                "image": _image("postgres", tag="16"),
                "port": 5432,
                "env": [{"name": "POSTGRES_USER", "default": "postgres"}],
                "secrets": [{"name": "POSTGRES_PASSWORD", "generate": {"bytes": 32}}],
                "volumes": [{"name": "data", "path": "/var/lib/postgresql/data"}],
                "healthcheck": {"command": ["pg_isready", "-U", "postgres"]},
            }
        },
        "provides": {"kind": "postgres", "version": 1, "container": "db"},
        "provision": {
            "per_consumer": "database_and_user",
            "image": _image("postgres", tag="16"),
            "script": "provision.sh",
            "admin_env": {"user": "POSTGRES_USER", "password": "POSTGRES_PASSWORD"},
        },
    }


def _without_mcp(document: dict) -> dict:
    for key in (
        "mcp",
        "identity",
        "recommended_policy",
        "public_paths",
        "public_base_url_env",
        "needs",
        "homepage",
    ):
        document.pop(key, None)
    document["compat"]["contracts"] = []
    return document


def test_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "https://keystone.codes/schemas/service-package-v1.json"
    Draft202012Validator.check_schema(schema)

    assert schema["additionalProperties"] is False
    mcp = schema["$defs"]["mcp"]["properties"]
    assert mcp["transport"]["default"] == "http"
    assert mcp["path"]["default"] == "/mcp"
    assert mcp["healthz"]["default"] == "/healthz"
    assert schema["$defs"]["container"]["properties"]["init"]["default"] is False
    health = schema["$defs"]["healthcheck"]["properties"]
    assert health["interval"]["default"] == "10s"
    assert health["timeout"]["default"] == "5s"
    assert health["start_period"]["default"] == "20s"
    assert health["retries"]["default"] == 5
    assert schema["$defs"]["envVar"]["properties"]["required"]["default"] is False


def test_valid_single_container_service() -> None:
    assert manifest_errors(_service()) == []


def test_valid_multi_container_service_with_init() -> None:
    assert manifest_errors(_multi()) == []


def test_valid_service_without_mcp() -> None:
    assert manifest_errors(_without_mcp(_service())) == []


def test_valid_s3_resource() -> None:
    assert manifest_errors(_resource()) == []


def test_valid_postgres_resource() -> None:
    assert manifest_errors(_postgres()) == []


def test_minimal_service() -> None:
    document = _service()
    for key in ("homepage", "needs", "public_paths", "public_base_url_env", "recommended_policy"):
        document.pop(key)
    document["containers"]["mcp"].pop("env")
    document["containers"]["mcp"].pop("secrets")
    assert manifest_errors(document) == []


def test_postgres_bind_env_url_form() -> None:
    assert manifest_errors(_multi()) == []


def test_postgres_bind_env_parts_form() -> None:
    document = _multi()
    document["needs"][0]["bind_env"] = dict(_POSTGRES_PARTS)
    assert manifest_errors(document) == []


def test_postgres_bind_env_both_forms() -> None:
    document = _multi()
    document["needs"][0]["bind_env"] = {"url": "DOCS_DATABASE_URL", **_POSTGRES_PARTS}
    assert manifest_errors(document) == []


def test_image_with_tag_and_digest_accepted() -> None:
    document = _service()
    document["containers"]["mcp"]["image"] = _image("ghcr.io/example/file-mcp", tag="1.2.3")
    assert manifest_errors(document) == []


def test_run_section_rejected() -> None:
    document = _service()
    document["run"] = {"image": "ghcr.io/example/file-mcp:1.0.0"}
    assert manifest_errors(document) == [
        "run: not supported in schema_version 1: describe containers under containers.<role>"
    ]


def test_image_without_digest_rejected() -> None:
    document = _service()
    document["containers"]["mcp"]["image"] = "ghcr.io/example/file-mcp:1.0.0"
    digest_error = (
        "containers.mcp.image: must be pinned by digest: "
        "ghcr.io/example/file-mcp:1.0.0@sha256:<64 hex>"
    )
    assert manifest_errors(document) == [digest_error]
    resource = _resource()
    resource["provision"]["image"] = "minio/mc:1.0.0"
    assert manifest_errors(resource) == [
        "provision.image: must be pinned by digest: minio/mc:1.0.0@sha256:<64 hex>"
    ]


def test_latest_tag_rejected() -> None:
    tagged = f"ghcr.io/example/file-mcp:latest@sha256:{DIGEST}"
    document = _service()
    document["containers"]["mcp"]["image"] = tagged
    assert manifest_errors(document) == [
        "containers.mcp.image: the tag 'latest' is not allowed; use a version tag with a digest"
    ]
    resource = _resource()
    resource["provision"]["image"] = f"minio/mc:latest@sha256:{DIGEST}"
    assert manifest_errors(resource) == [
        "provision.image: the tag 'latest' is not allowed; use a version tag with a digest"
    ]


def test_stdio_transport_rejected() -> None:
    document = _service()
    document["mcp"]["transport"] = "stdio"
    message = "mcp.transport: stdio packages are not supported in this release; use transport: http"
    errors = manifest_errors(document)
    assert errors == [message]
    assert len([item for item in errors if item.startswith("mcp.transport")]) == 1


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("ports", "services are reached only over the internal network"),
        ("expose", "services are reached only over the internal network"),
        ("build", "packages ship a pinned image"),
        *[(name, "packages cannot request host access") for name in sorted(FORBIDDEN_HOST_KEYS)],
        *[(name, "the installer sets it") for name in sorted(INSTALLER_KEYS)],
    ],
)
def test_forbidden_container_keys_rejected(key: str, reason: str) -> None:
    document = _service()
    document["containers"]["mcp"][key] = "no"
    assert manifest_errors(document) == [f"containers.mcp.{key} is not allowed: {reason}"]


@pytest.mark.parametrize(
    "volume",
    [
        "/var/run/docker.sock:/var/run/docker.sock",
        {"host": "/srv", "path": "/data"},
    ],
)
def test_host_volume_rejected(volume: object) -> None:
    document = _service()
    document["containers"]["mcp"]["volumes"] = [volume]
    assert manifest_errors(document) == [
        "containers.mcp.volumes[0] must be a named volume {name, path}; host paths are not allowed"
    ]


def test_unknown_top_level_key_rejected() -> None:
    document = _service()
    document["compose"] = {}
    assert manifest_errors(document) == ["$: additional properties are not allowed: compose"]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("File_MCP", f"name: {'File_MCP'!r} does not match {NAME_PATTERN!r}"),
        ("a", f"name: {'a'!r} does not match {NAME_PATTERN!r}"),
        ("x--y", f"name: {'x--y'!r} does not match {NAME_PATTERN!r}"),
        ("keystone", "name: names starting with 'keystone' are reserved"),
        ("keystone-files", "name: names starting with 'keystone' are reserved"),
    ],
)
def test_bad_name_rejected(name: str, message: str) -> None:
    document = _service()
    document["name"] = name
    assert manifest_errors(document) == [message]


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc.1"])
def test_bad_version_rejected(version: str) -> None:
    document = _service()
    document["version"] = version
    assert manifest_errors(document) == [f"version: {version!r} does not match {VERSION_PATTERN!r}"]


def test_no_containers_or_only_init_rejected() -> None:
    only_init = _without_mcp(_service())
    only_init["containers"] = {
        "migrate": {"image": _image("ghcr.io/example/job"), "init": True},
    }
    assert manifest_errors(only_init) == [
        "containers: at least one container without init: true is required"
    ]

    empty = _without_mcp(_service())
    empty["containers"] = {}
    empty_errors = manifest_errors(empty)
    assert "containers: at least one container without init: true is required" in empty_errors
    assert "containers: {} should be non-empty" in empty_errors

    missing = _without_mcp(_service())
    del missing["containers"]
    missing_errors = manifest_errors(missing)
    assert "containers: at least one container without init: true is required" in missing_errors
    assert "$: 'containers' is a required property" in missing_errors


def test_init_container_with_port_or_healthcheck_rejected() -> None:
    with_port = _multi()
    with_port["containers"]["migrate"]["port"] = 1234
    assert manifest_errors(with_port) == [
        "containers.migrate: init containers must not have port or healthcheck"
    ]
    with_health = _multi()
    with_health["containers"]["migrate"]["healthcheck"] = {"command": ["true"]}
    assert manifest_errors(with_health) == [
        "containers.migrate: init containers must not have port or healthcheck"
    ]


def test_long_running_without_healthcheck_rejected() -> None:
    document = _service()
    del document["containers"]["mcp"]["healthcheck"]
    assert manifest_errors(document) == ["containers.mcp.healthcheck is required"]


def test_mcp_container_unknown_init_or_portless_rejected() -> None:
    unknown = _service()
    unknown["mcp"]["container"] = "missing"
    assert manifest_errors(unknown) == [
        "mcp.container: 'missing' is not a container of this package"
    ]

    init_facade = _multi()
    init_facade["mcp"]["container"] = "migrate"
    init_errors = manifest_errors(init_facade)
    assert "mcp.container: 'migrate' is an init container" in init_errors
    assert "mcp.container: 'migrate' has no port" in init_errors

    portless = _service()
    del portless["containers"]["mcp"]["port"]
    portless_errors = manifest_errors(portless)
    assert "mcp.container: 'mcp' has no port" in portless_errors
    assert "containers.mcp.healthcheck.path requires containers.mcp.port" in portless_errors


def test_mcp_healthz_must_match_container_healthcheck() -> None:
    document = _service()
    document["containers"]["mcp"]["healthcheck"]["path"] = "/ready"
    assert manifest_errors(document) == ["containers.mcp.healthcheck.path must equal mcp.healthz"]
    document["mcp"]["healthz"] = "/ready"
    assert manifest_errors(document) == []


def test_critical_not_in_tools_rejected() -> None:
    document = _service()
    document["mcp"]["critical"] = ["not_a_tool"]
    assert manifest_errors(document) == ["mcp.critical: not_a_tool is not listed in mcp.tools"]


@pytest.mark.parametrize(
    ("namespace", "tool", "count"),
    [
        ("users", "users_add", 9),
        ("policy", "policy_allow_add", 9),
        ("api", "api_keys_issue", 3),
    ],
)
def test_reserved_namespace_rejected(namespace: str, tool: str, count: int) -> None:
    assert len(BUILTIN_TOOL_NAMES) == 24
    document = _service()
    document["mcp"]["namespace"] = namespace
    errors = manifest_errors(document)
    assert (
        f"mcp.namespace: '{namespace}' is reserved: published names {namespace}_* "
        f"can collide with keystone-mcp built-in tool {tool}"
    ) in errors
    assert len(errors) == count


def test_identity_required_with_mcp_and_forbidden_without() -> None:
    missing = _service()
    del missing["identity"]
    assert manifest_errors(missing) == ["identity is required when mcp is set"]

    extra = _without_mcp(_service())
    extra["identity"] = {
        "signed_assertion": "required",
        "secret_env": "FILE_MCP_IDENTITY_SECRET",
        "audience_env": "FILE_MCP_REGISTRY_NAME",
    }
    assert manifest_errors(extra) == ["identity requires mcp"]


def test_compat_contracts_required_with_mcp() -> None:
    document = _service()
    document["compat"]["contracts"] = ["upstream-registry/1"]
    message = (
        "compat.contracts must include upstream-registry/1 and identity-assertion/1 when mcp is set"
    )
    assert manifest_errors(document) == [message]


@pytest.mark.parametrize("field", ["compat", "mcp", "identity", "needs"])
def test_resource_with_service_fields_rejected(field: str) -> None:
    document = _resource()
    document[field] = {"present": True}
    assert manifest_errors(document) == [f"{field} is not allowed for kind: resource"]


def test_service_with_provides_rejected() -> None:
    document = _service()
    document["provides"] = {"kind": "s3", "version": 1, "container": "mcp"}
    assert manifest_errors(document) == ["provides is only allowed for kind: resource"]
    provisioned = _service()
    provisioned["provision"] = {
        "per_consumer": "bucket_and_user",
        "image": _image("minio/mc", tag=None),
        "script": "provision.sh",
        "admin_env": {"user": "MINIO_ROOT_USER", "password": "MINIO_ROOT_PASSWORD"},
    }
    assert manifest_errors(provisioned) == ["provision is only allowed for kind: resource"]


def test_needs_s3_missing_bind_env_key_rejected() -> None:
    document = _service()
    del document["needs"][0]["bind_env"]["endpoint"]
    assert manifest_errors(document) == ["needs[0].bind_env: 'endpoint' is a required property"]


def test_postgres_bind_env_partial_parts_rejected() -> None:
    message = (
        "needs[0].bind_env: use url, or all of host, port, database, user, password, "
        "sslmode, or both"
    )
    with_url = _multi()
    with_url["needs"][0]["bind_env"] = {"url": "DOCS_DATABASE_URL", "host": "DOCS_DB_HOST"}
    assert manifest_errors(with_url) == [message]
    host_only = _multi()
    host_only["needs"][0]["bind_env"] = {"host": "DOCS_DB_HOST"}
    assert manifest_errors(host_only) == [message]


def test_extensions_on_s3_rejected() -> None:
    document = _service()
    document["needs"][0]["extensions"] = ["pgcrypto"]
    assert manifest_errors(document) == [
        "needs[0].extensions: only postgres resources take extensions"
    ]


def test_duplicate_needs_kind_rejected() -> None:
    document = _service()
    extra = deepcopy(document["needs"][0])
    for key, value in extra["bind_env"].items():
        extra["bind_env"][key] = f"{value}_EXTRA"
    document["needs"].append(extra)
    assert manifest_errors(document) == ["needs[1].kind: only one s3 resource per package"]


def test_needs_containers_unknown_role_rejected() -> None:
    document = _service()
    document["needs"][0]["containers"] = ["missing"]
    assert manifest_errors(document) == [
        "needs[0].containers[0]: 'missing' is not a container of this package"
    ]


def test_duplicate_variable_in_container_rejected() -> None:
    document = _service()
    document["containers"]["mcp"]["env"].append({"name": "FILE_MCP_BEARER_TOKEN"})
    assert manifest_errors(document) == ["containers.mcp: duplicate variable FILE_MCP_BEARER_TOKEN"]
    other = _multi()
    other["containers"]["api"]["env"] = [{"name": "DOCS_MCP_BEARER"}]
    assert manifest_errors(other) == []


def test_shared_secret_declared_differently_rejected() -> None:
    document = _multi()
    document["containers"]["mcp"]["secrets"][0]["generate"]["bytes"] = 48
    assert manifest_errors(document) == [
        "containers.mcp.secrets[0]: DOCS_INTERNAL_TOKEN is declared differently in containers.api"
    ]


def test_public_paths_rules() -> None:
    missing_base = _service()
    del missing_base["public_base_url_env"]
    assert manifest_errors(missing_base) == [
        "public_base_url_env is required when public_paths are set"
    ]

    base_only = _service()
    del base_only["public_paths"]
    assert manifest_errors(base_only) == ["public_base_url_env requires public_paths"]

    no_facade = _service()
    del no_facade["mcp"]
    del no_facade["identity"]
    del no_facade["recommended_policy"]
    assert manifest_errors(no_facade) == [
        "public_container is required when public_paths are set and mcp is absent"
    ]

    overlap = _service()
    overlap["public_paths"] = ["/mcp/"]
    assert manifest_errors(overlap) == ["public_paths[0]: overlaps mcp.path"]


def test_provision_kind_mismatch_and_undeclared_admin_env_rejected() -> None:
    mismatch = _resource()
    mismatch["provision"]["per_consumer"] = "database_and_user"
    assert manifest_errors(mismatch) == [
        "provision.per_consumer: 'database_and_user' does not match provides.kind 's3'"
    ]

    missing_password = _resource()
    missing_password["containers"]["server"]["secrets"] = [
        {"name": "MINIO_ROOT_USER", "generate": {"bytes": 16}}
    ]
    password_error = (
        "provision.admin_env.password: MINIO_ROOT_PASSWORD is not declared in "
        "containers.server.secrets"
    )
    assert manifest_errors(missing_password) == [password_error]

    missing_user = _resource()
    missing_user["containers"]["server"]["secrets"] = [
        {"name": "MINIO_ROOT_PASSWORD", "generate": {"bytes": 32}}
    ]
    user_error = (
        "provision.admin_env.user: MINIO_ROOT_USER is not declared in "
        "containers.server.env or secrets"
    )
    assert manifest_errors(missing_user) == [user_error]


def test_env_reserved_prefix_and_required_with_default_rejected() -> None:
    prefixed = _service()
    prefixed["containers"]["mcp"]["env"] = [{"name": "KEYSTONE_MCP_TOKEN"}]
    assert manifest_errors(prefixed) == [
        "containers.mcp.env[0].name: must not start with KEYSTONE_MCP_ or KEYSTONE_SETUP_"
    ]
    setup = _service()
    setup["containers"]["mcp"]["env"] = [{"name": "KEYSTONE_SETUP_DOMAIN"}]
    assert manifest_errors(setup) == [
        "containers.mcp.env[0].name: must not start with KEYSTONE_MCP_ or KEYSTONE_SETUP_"
    ]
    both = _service()
    both["containers"]["mcp"]["env"] = [
        {"name": "FILE_MCP_MAX_BYTES", "required": True, "default": "1"}
    ]
    assert manifest_errors(both) == [
        "containers.mcp.env[0]: required and default are mutually exclusive"
    ]


@pytest.mark.parametrize("policy", ["../policy.yaml", "foo/../../policy.yaml", "/etc/policy.yaml"])
def test_recommended_policy_path_traversal_rejected(policy: str) -> None:
    document = _service()
    document["recommended_policy"] = policy
    assert manifest_errors(document) == [
        f"recommended_policy: {policy!r} does not match {POLICY_PATTERN!r}"
    ]


def test_requires_self_rejected() -> None:
    document = _service()
    document["requires"] = ["file-mcp"]
    assert manifest_errors(document) == ["requires: a package cannot require itself"]


def test_errors_are_reported_together() -> None:
    document = _service()
    document["version"] = "1.0"
    document["containers"]["mcp"]["image"] = "ghcr.io/example/file-mcp:1.0.0"
    del document["containers"]["mcp"]["healthcheck"]
    assert manifest_errors(document) == [
        "containers.mcp.healthcheck is required",
        (
            "containers.mcp.image: must be pinned by digest: "
            "ghcr.io/example/file-mcp:1.0.0@sha256:<64 hex>"
        ),
        f"version: {'1.0'!r} does not match {VERSION_PATTERN!r}",
    ]


def test_cli_exit_codes(tmp_path: Path) -> None:
    good = tmp_path / "good.keystone-service.yaml"
    bad = tmp_path / "bad.keystone-service.yaml"
    good.write_text(yaml.safe_dump(_service(), sort_keys=False), encoding="utf-8")
    invalid = _service()
    invalid["version"] = "1.0"
    bad.write_text(yaml.safe_dump(invalid, sort_keys=False), encoding="utf-8")

    passed = subprocess.run(
        [sys.executable, "-m", "keystonectl.manifest", str(good)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert passed.returncode == 0
    assert passed.stdout == ""

    failed = subprocess.run(
        [sys.executable, "-m", "keystonectl.manifest", str(bad)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1
    assert "version:" in failed.stdout
    assert failed.stderr == ""
