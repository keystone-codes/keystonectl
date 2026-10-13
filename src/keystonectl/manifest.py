"""Validate a keystone-service.yaml document against schema_version 1.

The module has no network access and no side effects beyond reading the
schema file and, for the command line, the manifest paths it is given.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from keystonectl.keystone_mcp_rules import reserved_namespace_errors

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schemas" / "service-package-v1.json"

FORBIDDEN_HOST_KEYS: frozenset[str] = frozenset(
    {
        "privileged",
        "cap_add",
        "devices",
        "network_mode",
        "pid",
        "ipc",
        "security_opt",
        "extra_hosts",
        "volumes_from",
        "sysctls",
        "userns_mode",
    }
)
INSTALLER_KEYS: frozenset[str] = frozenset(
    {
        "networks",
        "depends_on",
        "restart",
        "container_name",
        "links",
    }
)

_SERVICE_ONLY_FIELDS: tuple[str, ...] = (
    "compat",
    "mcp",
    "identity",
    "needs",
    "requires",
    "public_paths",
    "public_container",
    "public_base_url_env",
    "recommended_policy",
)
_RESOURCE_ONLY_FIELDS: tuple[str, ...] = ("provides", "provision")
_HOST_VOLUME_KEYS: frozenset[str] = frozenset({"host", "source", "type"})
_CONTAINER_KEY_REASONS: dict[str, str] = {
    "ports": "services are reached only over the internal network",
    "expose": "services are reached only over the internal network",
    "build": "packages ship a pinned image",
}
_POSTGRES_PARTS: tuple[str, ...] = ("host", "port", "database", "user", "password", "sslmode")
_CONTRACTS: tuple[str, ...] = ("upstream-registry/1", "identity-assertion/1")
_PROVISION_FOR_KIND: dict[str, str] = {
    "s3": "bucket_and_user",
    "postgres": "database_and_user",
}
_LATEST_TAG_RE = re.compile(r":latest(?:@|$)")
_RESERVED_NAME_RE = re.compile(r"^keystone(?:-|$)")

_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


class ManifestError(ValueError):
    """One or more manifest failures. ``messages`` is every error at once."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        super().__init__("\n".join(self.messages))


class _Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.prefixes: list[tuple[Any, ...]] = []
        self.blocked_extras: dict[tuple[Any, ...], set[str]] = {}
        self.false_instances: list[object] = []

    def add(self, path: str, message: str) -> None:
        self.items.append((path, message))

    def suppress(self, prefix: tuple[Any, ...]) -> None:
        self.prefixes.append(prefix)

    def block_extra(self, parent: tuple[Any, ...], key: str) -> None:
        self.blocked_extras.setdefault(parent, set()).add(key)


def manifest_errors(document: object) -> list[str]:
    """Return every problem in ``document``, sorted by path, without repeats."""
    if not isinstance(document, dict):
        return ["$: must be an object"]
    report = _Report()
    _friendly(document, report)
    _schema_errors(document, report)
    _semantic(document, report)
    return _finish(report.items)


def load_manifest(path: Path) -> dict[str, Any]:
    """Read one YAML manifest. Raise ``ManifestError`` on any failure."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError([f"{path}: file not found"]) from None
    except UnicodeDecodeError:
        raise ManifestError([f"{path}: invalid YAML: file is not valid UTF-8"]) from None
    except OSError as exc:
        detail = exc.strerror or "cannot read file"
        raise ManifestError([f"{path}: {detail}"]) from None
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError([f"{path}: invalid YAML: {exc}"]) from None
    if not isinstance(document, dict):
        raise ManifestError(["$: must be an object"])
    errors = manifest_errors(document)
    if errors:
        raise ManifestError(errors)
    return document


def main(argv: list[str] | None = None) -> int:
    """Check manifest files. Errors go to stdout. Exit 0 or 1."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m keystonectl.manifest FILE...")
        return 1
    failed = False
    for arg in args:
        try:
            load_manifest(Path(arg))
        except ManifestError as exc:
            failed = True
            for message in exc.messages:
                print(message)
    return 1 if failed else 0


def _friendly(document: dict[str, Any], report: _Report) -> None:
    if "run" in document:
        report.add(
            "run",
            "run: not supported in schema_version 1: describe containers under containers.<role>",
        )
        report.block_extra((), "run")

    kind = document.get("kind")
    if kind == "resource":
        for field in _SERVICE_ONLY_FIELDS:
            if field in document:
                report.add(field, f"{field} is not allowed for kind: resource")
                report.suppress((field,))
                report.false_instances.append(document[field])
    elif kind == "service":
        for field in _RESOURCE_ONLY_FIELDS:
            if field in document:
                report.add(field, f"{field} is only allowed for kind: resource")
                report.suppress((field,))
                report.false_instances.append(document[field])

    mcp = document.get("mcp")
    if isinstance(mcp, dict) and mcp.get("transport") == "stdio":
        report.add(
            "mcp.transport",
            "mcp.transport: stdio packages are not supported in this release; use transport: http",
        )
        report.suppress(("mcp", "transport"))

    containers = document.get("containers")
    if isinstance(containers, dict):
        for role, container in containers.items():
            if not isinstance(role, str) or not isinstance(container, dict):
                continue
            _friendly_container(role, container, report)

    _friendly_image(document.get("provision"), ("provision", "image"), "provision.image", report)


def _friendly_container(role: str, container: dict[str, Any], report: _Report) -> None:
    parent = ("containers", role)
    for key in container:
        if key in _CONTAINER_KEY_REASONS:
            reason = _CONTAINER_KEY_REASONS[key]
        elif key in FORBIDDEN_HOST_KEYS:
            reason = "packages cannot request host access"
        elif key in INSTALLER_KEYS:
            reason = "the installer sets it"
        else:
            continue
        path = f"containers.{role}.{key}"
        report.add(path, f"{path} is not allowed: {reason}")
        report.block_extra(parent, key)

    volumes = container.get("volumes")
    if isinstance(volumes, list):
        for index, item in enumerate(volumes):
            host_volume = isinstance(item, str) or (
                isinstance(item, dict) and bool(_HOST_VOLUME_KEYS.intersection(item))
            )
            if not host_volume:
                continue
            path = f"containers.{role}.volumes[{index}]"
            report.add(
                path,
                f"{path} must be a named volume {{name, path}}; host paths are not allowed",
            )
            report.suppress(("containers", role, "volumes", index))

    _friendly_image(
        container,
        ("containers", role, "image"),
        f"containers.{role}.image",
        report,
    )


def _friendly_image(
    owner: object,
    prefix: tuple[Any, ...],
    path: str,
    report: _Report,
) -> None:
    if not isinstance(owner, dict) or "image" not in owner:
        return
    image = owner.get("image")
    if not isinstance(image, str):
        return
    flagged = False
    if "@sha256:" not in image:
        report.add(path, f"{path}: must be pinned by digest: {image}@sha256:<64 hex>")
        flagged = True
    if _LATEST_TAG_RE.search(image):
        report.add(
            path,
            f"{path}: the tag 'latest' is not allowed; use a version tag with a digest",
        )
        flagged = True
    if flagged:
        report.suppress(prefix)


def _schema_errors(document: dict[str, Any], report: _Report) -> None:
    for error in _VALIDATOR.iter_errors(document):
        path_t = tuple(error.absolute_path)
        if _covered(path_t, report.prefixes):
            continue
        if error.schema is False and any(
            error.instance is value for value in report.false_instances
        ):
            continue
        if error.validator == "additionalProperties":
            _additional_properties(error, path_t, report)
            continue
        if error.validator == "oneOf" and _rewrite_oneof(error, path_t, report):
            continue
        path = _format_path(path_t)
        if (
            error.validator == "not"
            and path_t == ("name",)
            and isinstance(error.instance, str)
            and _RESERVED_NAME_RE.search(error.instance)
        ):
            report.add("name", "name: names starting with 'keystone' are reserved")
            continue
        if (
            error.validator == "not"
            and isinstance(error.instance, str)
            and (
                error.instance.startswith("KEYSTONE_MCP_")
                or error.instance.startswith("KEYSTONE_SETUP_")
            )
        ):
            report.add(path, f"{path}: must not start with KEYSTONE_MCP_ or KEYSTONE_SETUP_")
            continue
        report.add(path, f"{path}: {error.message}")


def _additional_properties(error: Any, path_t: tuple[Any, ...], report: _Report) -> None:
    if _covered(path_t, report.prefixes):
        return
    schema = error.schema if isinstance(error.schema, dict) else {}
    instance = error.instance if isinstance(error.instance, dict) else {}
    properties = schema.get("properties", {})
    patterns = [re.compile(pattern) for pattern in schema.get("patternProperties", {})]
    blocked = report.blocked_extras.get(path_t, set())
    extras: list[str] = []
    for key in instance:
        if not isinstance(key, str) or key in properties or key in blocked:
            continue
        if any(pattern.search(key) for pattern in patterns):
            continue
        extras.append(key)
    if not extras:
        return
    path = _format_path(path_t)
    report.add(path, f"{path}: additional properties are not allowed: {', '.join(extras)}")


def _rewrite_oneof(error: Any, path_t: tuple[Any, ...], report: _Report) -> bool:
    """Replace selected oneOf failures. Return True when the error is consumed."""
    path = _format_path(path_t)
    if _is_secret_path(path_t):
        if isinstance(error.instance, dict):
            report.add(
                path,
                f"{path}: use either generate: {{bytes: N}} or required: true",
            )
        return True
    if _is_postgres_bind_path(path_t):
        if isinstance(error.instance, dict):
            report.add(
                path,
                f"{path}: use url, or all of host, port, database, user, password, "
                "sslmode, or both",
            )
        return True
    return False


def _semantic(document: dict[str, Any], report: _Report) -> None:
    containers = document.get("containers")
    _require_long_running(containers, report)
    if isinstance(containers, dict):
        for role, container in containers.items():
            if isinstance(role, str) and isinstance(container, dict):
                _semantic_container(role, container, document, report)
        _shared_secrets(containers, report)
    kind = document.get("kind")
    if kind == "service":
        _semantic_service(document, report)
    elif kind == "resource":
        _semantic_resource(document, report)


def _require_long_running(containers: object, report: _Report) -> None:
    if not isinstance(containers, dict) or not containers:
        report.add(
            "containers",
            "containers: at least one container without init: true is required",
        )
        return
    for container in containers.values():
        if isinstance(container, dict) and container.get("init") is not True:
            return
    report.add(
        "containers",
        "containers: at least one container without init: true is required",
    )


def _semantic_container(
    role: str,
    container: dict[str, Any],
    document: dict[str, Any],
    report: _Report,
) -> None:
    init = container.get("init") is True
    if init:
        if "port" in container or "healthcheck" in container:
            report.add(
                f"containers.{role}",
                f"containers.{role}: init containers must not have port or healthcheck",
            )
    else:
        healthcheck = container.get("healthcheck")
        if not isinstance(healthcheck, dict):
            report.add(
                f"containers.{role}.healthcheck",
                f"containers.{role}.healthcheck is required",
            )
        elif "path" in healthcheck and "port" not in container:
            report.add(
                f"containers.{role}.healthcheck.path",
                f"containers.{role}.healthcheck.path requires containers.{role}.port",
            )
    _env_required_and_default(role, container.get("env"), report)
    _duplicate_volumes(role, container.get("volumes"), report)
    _duplicate_variables(role, container, document, report)


def _env_required_and_default(role: str, env: object, report: _Report) -> None:
    if not isinstance(env, list):
        return
    for index, item in enumerate(env):
        if isinstance(item, dict) and item.get("required") is True and "default" in item:
            path = f"containers.{role}.env[{index}]"
            report.add(path, f"{path}: required and default are mutually exclusive")


def _duplicate_volumes(role: str, volumes: object, report: _Report) -> None:
    if not isinstance(volumes, list):
        return
    names: set[str] = set()
    paths: set[str] = set()
    for item in volumes:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        mount = item.get("path")
        if isinstance(name, str):
            if name in names:
                report.add(
                    f"containers.{role}.volumes",
                    f"containers.{role}.volumes: duplicate name {name}",
                )
            names.add(name)
        if isinstance(mount, str):
            if mount in paths:
                report.add(
                    f"containers.{role}.volumes",
                    f"containers.{role}.volumes: duplicate path {mount}",
                )
            paths.add(mount)


def _duplicate_variables(
    role: str,
    container: dict[str, Any],
    document: dict[str, Any],
    report: _Report,
) -> None:
    seen: set[str] = set()

    def take(name: object) -> None:
        if not isinstance(name, str):
            return
        if name in seen:
            report.add(f"containers.{role}", f"containers.{role}: duplicate variable {name}")
        seen.add(name)

    for name in _entry_names(container.get("env")):
        take(name)
    for name in _entry_names(container.get("secrets")):
        take(name)
    for name in _bind_names(role, document):
        take(name)
    for name in _installer_names(role, container, document):
        take(name)


def _entry_names(entries: object) -> list[str]:
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _bind_names(role: str, document: dict[str, Any]) -> list[str]:
    needs = document.get("needs")
    if not isinstance(needs, list):
        return []
    names: list[str] = []
    for item in needs:
        if not isinstance(item, dict):
            continue
        targets = item.get("containers")
        if isinstance(targets, list):
            if role not in targets:
                continue
        elif "containers" in item:
            continue
        bind_env = item.get("bind_env")
        if not isinstance(bind_env, dict):
            continue
        for value in bind_env.values():
            if isinstance(value, str):
                names.append(value)
    return names


def _installer_names(role: str, container: dict[str, Any], document: dict[str, Any]) -> list[str]:
    names: list[str] = []
    mcp = document.get("mcp")
    if isinstance(mcp, dict) and mcp.get("container") == role:
        bearer = mcp.get("bearer_env")
        if isinstance(bearer, str):
            names.append(bearer)
        identity = document.get("identity")
        if isinstance(identity, dict):
            for key in ("secret_env", "audience_env"):
                value = identity.get(key)
                if isinstance(value, str):
                    names.append(value)
    if container.get("init") is not True:
        public_paths = document.get("public_paths")
        public_env = document.get("public_base_url_env")
        if isinstance(public_paths, list) and public_paths and isinstance(public_env, str):
            names.append(public_env)
    return names


def _shared_secrets(containers: dict[str, Any], report: _Report) -> None:
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    for role, container in containers.items():
        if not isinstance(role, str) or not isinstance(container, dict):
            continue
        secrets = container.get("secrets")
        if not isinstance(secrets, list):
            continue
        for index, secret in enumerate(secrets):
            if not isinstance(secret, dict) or not isinstance(secret.get("name"), str):
                continue
            name = secret["name"]
            previous = seen.get(name)
            if previous is None:
                seen[name] = (role, secret)
                continue
            other_role, other = previous
            if secret != other:
                path = f"containers.{role}.secrets[{index}]"
                report.add(
                    path,
                    f"{path}: {name} is declared differently in containers.{other_role}",
                )


def _semantic_service(document: dict[str, Any], report: _Report) -> None:
    containers = document.get("containers")
    mcp = document.get("mcp")
    mcp_set = isinstance(mcp, dict)
    if mcp_set:
        assert isinstance(mcp, dict)
        _check_role(containers, "mcp.container", mcp.get("container"), report, port=True)
        _healthz_matches(mcp, containers, report)
        _critical_tools(mcp, report)
        namespace = mcp.get("namespace")
        if isinstance(namespace, str):
            for message in reserved_namespace_errors(namespace):
                report.add("mcp.namespace", message)
        if "identity" not in document:
            report.add("identity", "identity is required when mcp is set")
        _contracts(document.get("compat"), report)
    elif "identity" in document:
        report.add("identity", "identity requires mcp")
    if "recommended_policy" in document and not mcp_set:
        report.add("recommended_policy", "recommended_policy requires mcp")
    _public_paths(document, mcp if mcp_set else None, report)
    _needs(document, containers, report)
    _requires_self(document, report)


def _healthz_matches(mcp: dict[str, Any], containers: object, report: _Report) -> None:
    role = mcp.get("container")
    if not isinstance(role, str) or not isinstance(containers, dict):
        return
    container = containers.get(role)
    if not isinstance(container, dict):
        return
    healthcheck = container.get("healthcheck")
    if not isinstance(healthcheck, dict) or "path" not in healthcheck:
        return
    actual = healthcheck.get("path")
    if not isinstance(actual, str):
        return
    expected = mcp.get("healthz", "/healthz")
    if isinstance(expected, str) and actual != expected:
        report.add(
            f"containers.{role}.healthcheck.path",
            f"containers.{role}.healthcheck.path must equal mcp.healthz",
        )


def _critical_tools(mcp: dict[str, Any], report: _Report) -> None:
    tools = mcp.get("tools")
    critical = mcp.get("critical")
    if not isinstance(tools, list) or not isinstance(critical, list):
        return
    known = set(tools)
    for tool in critical:
        if isinstance(tool, str) and tool not in known:
            report.add("mcp.critical", f"mcp.critical: {tool} is not listed in mcp.tools")


def _contracts(compat: object, report: _Report) -> None:
    contracts: list[object] = []
    if isinstance(compat, dict) and isinstance(compat.get("contracts"), list):
        contracts = list(compat["contracts"])
    if any(name not in contracts for name in _CONTRACTS):
        report.add(
            "compat.contracts",
            "compat.contracts must include upstream-registry/1 and "
            "identity-assertion/1 when mcp is set",
        )


def _public_paths(document: dict[str, Any], mcp: dict[str, Any] | None, report: _Report) -> None:
    raw_paths = document.get("public_paths")
    paths_set = isinstance(raw_paths, list) and bool(raw_paths)
    base_set = isinstance(document.get("public_base_url_env"), str)
    container_set = "public_container" in document
    if paths_set and not base_set:
        report.add(
            "public_base_url_env",
            "public_base_url_env is required when public_paths are set",
        )
    if base_set and not paths_set:
        report.add("public_base_url_env", "public_base_url_env requires public_paths")
    if paths_set and mcp is None and not container_set:
        report.add(
            "public_container",
            "public_container is required when public_paths are set and mcp is absent",
        )
    if container_set and not paths_set:
        report.add("public_container", "public_container requires public_paths")
    if not paths_set or mcp is None or not isinstance(raw_paths, list):
        return
    facade = mcp.get("container")
    public = document["public_container"] if container_set else facade
    if not isinstance(facade, str) or public != facade:
        return
    mcp_path = _string_or_default(mcp, "path", "/mcp")
    mcp_healthz = _string_or_default(mcp, "healthz", "/healthz")
    for index, item in enumerate(raw_paths):
        if not isinstance(item, str):
            continue
        segment = _first_segment(item)
        if mcp_path is not None and segment == _first_segment(mcp_path) and segment:
            path = f"public_paths[{index}]"
            report.add(path, f"{path}: overlaps mcp.path")
        if mcp_healthz is not None and segment == _first_segment(mcp_healthz) and segment:
            path = f"public_paths[{index}]"
            report.add(path, f"{path}: overlaps mcp.healthz")


def _needs(document: dict[str, Any], containers: object, report: _Report) -> None:
    needs = document.get("needs")
    if not isinstance(needs, list):
        return
    roles = set(containers) if isinstance(containers, dict) else set()
    seen_kinds: set[str] = set()
    for index, item in enumerate(needs):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind in ("s3", "postgres"):
            if kind in seen_kinds:
                path = f"needs[{index}].kind"
                report.add(path, f"{path}: only one {kind} resource per package")
            else:
                seen_kinds.add(kind)
        if kind == "s3" and "extensions" in item:
            path = f"needs[{index}].extensions"
            report.add(path, f"{path}: only postgres resources take extensions")
        targets = item.get("containers")
        if not isinstance(targets, list):
            continue
        for target_index, role in enumerate(targets):
            if isinstance(role, str) and role not in roles:
                path = f"needs[{index}].containers[{target_index}]"
                report.add(path, f"{path}: '{role}' is not a container of this package")


def _requires_self(document: dict[str, Any], report: _Report) -> None:
    name = document.get("name")
    requires = document.get("requires")
    if isinstance(name, str) and isinstance(requires, list) and name in requires:
        report.add("requires", "requires: a package cannot require itself")


def _semantic_resource(document: dict[str, Any], report: _Report) -> None:
    containers = document.get("containers")
    provides = document.get("provides")
    provision = document.get("provision")
    provides_kind = provides.get("kind") if isinstance(provides, dict) else None
    role = provides.get("container") if isinstance(provides, dict) else None
    if isinstance(provides, dict):
        _check_role(containers, "provides.container", role, report, port=True)
    if not isinstance(provision, dict):
        return
    per_consumer = provision.get("per_consumer")
    expected = _PROVISION_FOR_KIND.get(provides_kind) if isinstance(provides_kind, str) else None
    if expected is not None and isinstance(per_consumer, str) and per_consumer != expected:
        report.add(
            "provision.per_consumer",
            f"provision.per_consumer: '{per_consumer}' does not match provides.kind "
            f"'{provides_kind}'",
        )
    _admin_env(provision.get("admin_env"), containers, role, report)


def _admin_env(
    admin_env: object,
    containers: object,
    role: object,
    report: _Report,
) -> None:
    if not isinstance(admin_env, dict) or not isinstance(role, str):
        return
    if not isinstance(containers, dict) or not isinstance(containers.get(role), dict):
        return
    container = containers[role]
    env_names = set(_entry_names(container.get("env")))
    secret_names = set(_entry_names(container.get("secrets")))
    user = admin_env.get("user")
    password = admin_env.get("password")
    if isinstance(user, str) and user not in env_names and user not in secret_names:
        report.add(
            "provision.admin_env.user",
            f"provision.admin_env.user: {user} is not declared in containers.{role}.env or secrets",
        )
    if isinstance(password, str) and password not in secret_names:
        report.add(
            "provision.admin_env.password",
            f"provision.admin_env.password: {password} is not declared in "
            f"containers.{role}.secrets",
        )


def _check_role(
    containers: object,
    path: str,
    role: object,
    report: _Report,
    *,
    port: bool,
) -> None:
    if not isinstance(role, str):
        return
    if not isinstance(containers, dict) or role not in containers:
        report.add(path, f"{path}: '{role}' is not a container of this package")
        return
    if not port:
        return
    target = containers[role]
    if not isinstance(target, dict):
        return
    if target.get("init") is True:
        report.add(path, f"{path}: '{role}' is an init container")
    if "port" not in target:
        report.add(path, f"{path}: '{role}' has no port")


def _string_or_default(mapping: dict[str, Any], key: str, default: str) -> str | None:
    if key not in mapping:
        return default
    value = mapping[key]
    return value if isinstance(value, str) else None


def _first_segment(path: str) -> str:
    stripped = path.strip("/")
    if not stripped:
        return ""
    return stripped.split("/", 1)[0]


def _is_secret_path(path: tuple[Any, ...]) -> bool:
    return (
        len(path) == 4
        and path[0] == "containers"
        and path[2] == "secrets"
        and isinstance(path[3], int)
    )


def _is_postgres_bind_path(path: tuple[Any, ...]) -> bool:
    return (
        len(path) == 3 and path[0] == "needs" and isinstance(path[1], int) and path[2] == "bind_env"
    )


def _covered(path: tuple[Any, ...], prefixes: list[tuple[Any, ...]]) -> bool:
    return any(path[: len(prefix)] == prefix for prefix in prefixes)


def _format_path(parts: tuple[Any, ...]) -> str:
    if not parts:
        return "$"
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, int):
            chunks.append(f"[{part}]")
        else:
            if chunks:
                chunks.append(".")
            chunks.append(str(part))
    return "".join(chunks)


def _path_sort_key(path: str) -> tuple[tuple[int, Any], ...]:
    if path == "$":
        return ()
    tokens: list[tuple[int, Any]] = []
    buffer: list[str] = []

    def flush_name() -> None:
        if buffer:
            tokens.append((0, "".join(buffer)))
            buffer.clear()

    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            flush_name()
            index += 1
        elif char == "[":
            flush_name()
            end = path.find("]", index)
            tokens.append((1, int(path[index + 1 : end])))
            index = end + 1
        else:
            buffer.append(char)
            index += 1
    flush_name()
    return tuple(tokens)


def _finish(items: list[tuple[str, str]]) -> list[str]:
    ordered = sorted(items, key=lambda item: (_path_sort_key(item[0]), item[1]))
    seen: set[str] = set()
    messages: list[str] = []
    for _, message in ordered:
        if message in seen:
            continue
        seen.add(message)
        messages.append(message)
    return messages


if __name__ == "__main__":
    raise SystemExit(main())
