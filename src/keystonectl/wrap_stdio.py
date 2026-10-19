"""Write a stdio-box package from a provider directory."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from packaging.version import InvalidVersion, Version

from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.generate.files import atomic_write
from keystonectl.lint import lint_manifest
from keystonectl.output import Result
from keystonectl.resources import schema_text

STDIO_BOX_IMAGE = (
    "ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:"
    "4d6081cb1608c922c6c7299d30570a145187e65e4be274f9bb98aaaa7d67c519"
)
STDIO_BOX_REF = "v0.1.0"
PROVIDER_SCHEMA_SHA256 = "4ffc23499879d983dd0fb8028c6f91a92fd6f4799cdd99139b02cc77d99db2b0"
_DIGEST_PLACEHOLDER = "0" * 64
_PUBLISHER = "keystone-codes"
_PACKAGE_VERSION = "1.0.0"
_DISCOVERY_TIMEOUT = 600
_BOX_TOOLS_TIMEOUT = 300
STOCK_PROVIDER_DIR = "/opt/box/provider"
_STDERR_TAIL = 20

_PACKAGE_NAME_RE = re.compile(r"^(?!.*--)[a-z][a-z0-9-]{1,38}[a-z0-9]$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RESERVED_NAME_RE = re.compile(r"^keystone(?:-|$)")
_PUBLISHED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SNAKE_RE = re.compile(r"[^A-Za-z0-9]+")
_FIELD_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_YAML_BOOL = {"true", "false", "null", "yes", "no", "on", "off"}

_NPM_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
)
_PY_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_NPM_NAME_RE = re.compile(r"^(?:@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|[A-Za-z0-9._-]+)$")
_NPM_YES = {"-y", "--yes"}

# The same patterns the manifest schema uses for these fields.
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PUBLISHER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_HOMEPAGE_RE = re.compile(r"^https://\S+$")
_BOX_TOOL_RE = re.compile(r"^creds_[a-z0-9_]{1,58}$")

# Fallback when the image cannot list its own tools (stdio-box 0.1.0 has no box-tools).
CREDS_TOOLS = (
    "creds_providers",
    "creds_profiles",
    "creds_bindings",
    "creds_profile_create",
    "creds_profile_rotate",
    "creds_profile_delete",
    "creds_bind",
    "creds_unbind",
    "creds_whoami",
    "creds_check",
)
CHANGING_CREDS = (
    "creds_profile_create",
    "creds_profile_rotate",
    "creds_profile_delete",
    "creds_bind",
    "creds_unbind",
)
_POLICY_CREDS = ("creds_whoami", "creds_providers")

_SCHEMA_VALIDATOR: Draft202012Validator | None = None


class _Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 300,
    ) -> Any: ...


@dataclass
class _Invalid(Exception):
    problems: list[dict[str, str]]


class _Unrecognized(Exception):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(path)


@dataclass(frozen=True)
class _Published:
    name: str
    read_only: bool
    destructive: bool


def run_wrap_stdio(
    provider_dir: Path,
    *,
    image: str,
    tools_snapshot: Path | None,
    no_discovery: bool,
    name: str | None,
    namespace: str | None,
    derived_image: bool,
    force: bool,
    runner: _Runner,
    stock_image: bool = False,
    publisher: str | None = None,
    homepage: str | None = None,
    version: str | None = None,
    read_only: Sequence[str] | None = None,
    destructive: Sequence[str] | None = None,
) -> Result:
    """Validate a provider directory and write its box package files."""
    command = "package wrap-stdio"
    directory = Path(provider_dir)
    usage = _usage_problem(
        stock_image=stock_image,
        derived_image=derived_image,
        publisher=publisher,
        homepage=homepage,
        version=version,
    )
    if usage is not None:
        return _failed(command, directory, "usage", message=usage)
    try:
        document = _load_provider(directory)
        parsed = _parse_box_command(str(document["server"]["command"]))
        package, namespace_name = _package_identity(document, name=name, namespace=namespace)
    except _Invalid as exc:
        return _failed(command, directory, "provider_invalid", problems=exc.problems)

    try:
        snapshot, discovery = _obtain_snapshot(
            directory,
            document,
            image=image,
            tools_snapshot=tools_snapshot,
            no_discovery=no_discovery,
            runner=runner,
        )
    except _Invalid as exc:
        first = exc.problems[0]
        if first.get("id") in {"docker_unavailable", "discovery_failed"}:
            return _failed(
                command,
                directory,
                str(first["id"]),
                message=first.get("message"),
                hint=first.get("hint"),
                retryable=first.get("retryable"),
                stderr=first.get("stderr"),
            )
        return _failed(command, directory, "provider_invalid", problems=exc.problems)

    try:
        published = [] if snapshot is None else _published_tools(snapshot, document)
        published = _override_hints(published, read_only=read_only, destructive=destructive)
    except _Invalid as exc:
        return _failed(command, directory, "provider_invalid", problems=exc.problems)

    box_tools, box_tools_source = _box_tools(image, runner)
    tool_names = [item.name for item in published] + box_tools
    critical = [*(item.name for item in published if item.destructive), *CHANGING_CREDS]
    warnings = []
    if discovery == "skipped":
        spec = WARNINGS["tools_snapshot_missing"]
        warnings.append({"id": spec.id, "message": spec.title})

    files: list[str] = []
    snapshot_path = directory / "tools.snapshot.json"
    if snapshot is not None:
        _write_text(snapshot_path, snapshot["text"])
        files.append("tools.snapshot.json")
    elif not snapshot_path.exists():
        # stdio-box does not start without a snapshot; an empty one publishes no server tools.
        _write_text(snapshot_path, _empty_snapshot_text(document))
        files.append("tools.snapshot.json")

    manifest_path = directory / "keystone-service.yaml"
    command_text = str(document["server"]["command"])
    image_ref = (
        f"ghcr.io/{_PUBLISHER}/{package}@sha256:{_DIGEST_PLACEHOLDER}" if derived_image else image
    )
    try:
        if manifest_path.is_file() and not force:
            updated = _update_manifest_text(
                manifest_path.read_text(encoding="utf-8"),
                tools=tool_names,
                critical=critical,
                command=command_text,
                stock_image=stock_image,
                publisher=publisher,
                homepage=homepage,
                version=version,
            )
            _write_text(manifest_path, updated)
        else:
            _write_text(
                manifest_path,
                _render_manifest(
                    document,
                    package=package,
                    namespace=namespace_name,
                    tools=tool_names,
                    critical=critical,
                    image=image_ref,
                    derived_image=derived_image,
                    stock_image=stock_image,
                    parsed=parsed,
                    publisher=publisher,
                    homepage=homepage,
                    version=version,
                ),
            )
    except _Unrecognized as exc:
        spec = ERRORS["manifest_unrecognized"]
        return _failed(
            command,
            directory,
            "manifest_unrecognized",
            message=f"{spec.title} ({exc.path})",
        )
    files.append("keystone-service.yaml")

    policy_path = directory / "policy.example.yaml"
    _write_text(
        policy_path,
        _render_policy(
            document,
            namespace=namespace_name,
            published=published,
        ),
    )
    files.append("policy.example.yaml")
    if derived_image:
        _write_text(
            directory / "Dockerfile",
            f"FROM {image}\nCOPY . /opt/box/packages/{package}/\n",
        )
        _write_text(
            directory / ".dockerignore",
            "keystone-service.yaml\npolicy.example.yaml\n",
        )
        files.extend(["Dockerfile", ".dockerignore"])

    problems, lint_warnings = lint_manifest(
        manifest_path,
        directory,
        keystone_version=None,
        check_files=True,
    )
    for item in lint_warnings:
        warnings.append({"id": item["id"], "message": item["message"]})
    if any(item["severity"] == "error" for item in problems):
        spec = ERRORS["manifest_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=str(directory),
            warnings=warnings,
            error={"id": spec.id, "message": spec.title, "hint": spec.action},
            retryable=spec.retryable,
            result={
                "problems": problems,
                "package": package,
                "namespace": namespace_name,
                "dir": str(directory),
                "files": files,
                "box_tools_source": box_tools_source,
            },
        )
    summary = f"Wrote package {package} ({len(tool_names)} tools)."
    return Result(
        code=0,
        command=command,
        summary=summary,
        target=str(directory),
        warnings=warnings,
        changes=len(files),
        result={
            "package": package,
            "namespace": namespace_name,
            "dir": str(directory),
            "files": files,
            "tools": len(tool_names),
            "discovery": discovery,
            "box_tools_source": box_tools_source,
            "warnings": warnings,
        },
    )


def _usage_problem(
    *,
    stock_image: bool,
    derived_image: bool,
    publisher: str | None,
    homepage: str | None,
    version: str | None,
) -> str | None:
    if stock_image and derived_image:
        return "--stock-image and --derived-image cannot be used together."
    if version is not None and _VERSION_RE.fullmatch(version) is None:
        return f"--version {version!r} is not a semantic version X.Y.Z."
    if publisher is not None and _PUBLISHER_RE.fullmatch(publisher) is None:
        return f"--publisher {publisher!r} is not a valid publisher name."
    if homepage is not None and _HOMEPAGE_RE.fullmatch(homepage) is None:
        return f"--homepage {homepage!r} is not an https:// address."
    return None


def _failed(
    command: str,
    directory: Path,
    error_id: str,
    *,
    problems: list[dict[str, str]] | None = None,
    message: str | None = None,
    hint: str | None = None,
    retryable: bool | None = None,
    stderr: str | None = None,
) -> Result:
    if error_id == "docker_unavailable":
        title = "Docker is needed to ask the server for its tools."
        action = "Install Docker, or pass --tools-snapshot with a snapshot taken elsewhere."
        code = 3
        retry = False if retryable is None else bool(retryable)
    else:
        spec = ERRORS[error_id]
        title = spec.title
        action = spec.action
        code = spec.code
        retry = spec.retryable if retryable is None else bool(retryable)
    payload: dict[str, Any] = {}
    if problems:
        payload["problems"] = problems
    if stderr is not None:
        payload["stderr"] = stderr
    return Result(
        code=code,
        command=command,
        summary=message or title,
        target=str(directory),
        error={"id": error_id, "message": message or title, "hint": hint or action},
        retryable=retry,
        result=payload,
    )


def _problem(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message, "severity": "error"}


def _load_provider(directory: Path) -> dict[str, Any]:
    if not directory.is_dir():
        raise _Invalid([_problem(str(directory), "provider directory does not exist")])
    path = directory / "provider.yaml"
    if not path.is_file():
        raise _Invalid([_problem("provider.yaml", "file not found")])
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _Invalid([_problem("provider.yaml", f"invalid YAML: {exc}")]) from exc
    if not isinstance(loaded, dict):
        raise _Invalid([_problem("provider.yaml", "document must be a mapping")])
    problems = [_schema_problem(item) for item in _validator().iter_errors(loaded)]
    if not problems:
        problems.extend(_semantic_problems(directory, loaded))
    if problems:
        raise _Invalid(problems)
    return loaded


def _validator() -> Draft202012Validator:
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        schema = json.loads(schema_text("stdio-provider-v1.json"))
        _SCHEMA_VALIDATOR = Draft202012Validator(schema)
    return _SCHEMA_VALIDATOR


def _schema_problem(error: ValidationError) -> dict[str, str]:
    if error.validator in {"oneOf", "anyOf"} and error.context:
        return _schema_problem(error.context[0])
    path = ""
    for item in error.absolute_path:
        if isinstance(item, int):
            path += f"[{item}]"
        else:
            path = f"{path}.{item}" if path else str(item)
    return _problem(path or "<root>", error.message)


def _semantic_problems(directory: Path, document: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    name = document["name"]
    if name != directory.name:
        problems.append(_problem("name", f"must equal directory name '{directory.name}'"))
    server = document["server"]
    command = server["command"]
    lock_name = server["lock"]
    if lock_name == "requirements.txt" and not str(command).startswith("uvx"):
        problems.append(
            _problem("server.command", "requirements.txt requires a command starting with 'uvx'")
        )
    if lock_name == "package-lock.json" and not str(command).startswith("npx"):
        problems.append(
            _problem("server.command", "package-lock.json requires a command starting with 'npx'")
        )
    if not (directory / lock_name).is_file():
        problems.append(_problem("server.lock", f"'{lock_name}' does not exist"))
    if lock_name == "package-lock.json" and not (directory / "package.json").is_file():
        problems.append(_problem("server.lock", "package.json must exist beside package-lock.json"))

    fields = document["fields"]
    fields_by_name: dict[str, dict[str, Any]] = {}
    seen_fields: set[str] = set()
    for index, field in enumerate(fields):
        field_name = field["name"]
        if field_name in seen_fields:
            problems.append(
                _problem(f"fields[{index}].name", f"duplicate field name '{field_name}'")
            )
        seen_fields.add(field_name)
        fields_by_name[field_name] = field
        target = field.get("to", "env")
        arg = None if target == "env" else target["arg"]
        if field["secret"] and arg is not None:
            problems.append(
                _problem(
                    f"fields[{index}].to",
                    f"secret field {field_name} cannot be passed as an argument",
                )
            )

    seen_variants: set[str] = set()
    seen_field_sets: dict[frozenset[str], str] = {}
    for index, variant in enumerate(document["variants"]):
        variant_name = variant["name"]
        if variant_name in seen_variants:
            problems.append(
                _problem(f"variants[{index}].name", f"duplicate variant name '{variant_name}'")
            )
        seen_variants.add(variant_name)
        variant_fields = tuple(variant["fields"])
        for field_index, field_name in enumerate(variant_fields):
            if field_name not in fields_by_name:
                problems.append(
                    _problem(
                        f"variants[{index}].fields[{field_index}]",
                        f"unknown field '{field_name}'",
                    )
                )
        field_set = frozenset(variant_fields)
        previous = seen_field_sets.get(field_set)
        if previous is not None:
            problems.append(
                _problem(
                    f"variants[{index}].fields",
                    f"variant '{variant_name}' has the same fields as variant '{previous}'",
                )
            )
        else:
            seen_field_sets[field_set] = variant_name
        variant_field_set = set(variant_fields)
        if "allowed_hosts" in variant and not any(
            fields_by_name[item]["type"] == "url"
            for item in variant_field_set
            if item in fields_by_name
        ):
            problems.append(
                _problem(
                    f"variants[{index}].allowed_hosts",
                    "allowed_hosts requires a field of type url",
                )
            )
        for check_index, check in enumerate(variant["checks"]):
            if "http" in check:
                _check_http(
                    f"variants[{index}].checks[{check_index}]",
                    check["http"],
                    variant_name,
                    variant_field_set,
                    fields_by_name,
                    problems,
                )

    discovery = document["discovery"]
    if discovery["mode"] == "placeholder":
        env = discovery.get("env") or {}
        covered = any(
            all(field_name in env and env[field_name] != "" for field_name in variant["fields"])
            for variant in document["variants"]
        )
        if not covered:
            problems.append(
                _problem(
                    "discovery.env",
                    "placeholder values must cover every field of at least one variant",
                )
            )
        for field_name, value in env.items():
            field = fields_by_name.get(field_name)
            if field is None or field["type"] != "url":
                continue
            host = urlsplit(str(value)).hostname
            if not host or not host.endswith(".invalid"):
                problems.append(
                    _problem(f"discovery.env.{field_name}", "host must end with .invalid")
                )
    return problems


def _check_http(
    prefix: str,
    http: dict[str, Any],
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict[str, Any]],
    problems: list[dict[str, str]],
) -> None:
    _check_interpolation(
        f"{prefix}.http.url",
        str(http["url"]),
        variant_name,
        variant_fields,
        fields_by_name,
        problems,
    )
    headers = http.get("headers") or {}
    for header_name, header_value in headers.items():
        _check_interpolation(
            f"{prefix}.http.headers.{header_name}",
            str(header_value),
            variant_name,
            variant_fields,
            fields_by_name,
            problems,
        )
    auth = http.get("auth")
    if auth is None:
        return
    if "basic" in auth:
        # The first basic slot is the username and may be non-secret.
        _require_field(
            f"{prefix}.http.auth.basic[0]",
            auth["basic"][0],
            variant_name,
            variant_fields,
            fields_by_name,
            problems,
            secret=False,
        )
        _require_field(
            f"{prefix}.http.auth.basic[1]",
            auth["basic"][1],
            variant_name,
            variant_fields,
            fields_by_name,
            problems,
            secret=True,
        )
    elif "bearer" in auth:
        _require_field(
            f"{prefix}.http.auth.bearer",
            auth["bearer"],
            variant_name,
            variant_fields,
            fields_by_name,
            problems,
            secret=True,
        )
    else:
        _require_field(
            f"{prefix}.http.auth.header.field",
            auth["header"]["field"],
            variant_name,
            variant_fields,
            fields_by_name,
            problems,
            secret=True,
        )


def _check_interpolation(
    path: str,
    text: str,
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict[str, Any]],
    problems: list[dict[str, str]],
) -> None:
    for token in _FIELD_TOKEN_RE.findall(text):
        field = fields_by_name.get(token)
        if field is None:
            problems.append(_problem(path, f"unknown field '{token}'"))
            continue
        if token not in variant_fields:
            problems.append(
                _problem(path, f"field '{token}' is not part of variant '{variant_name}'")
            )
            continue
        if field["secret"]:
            problems.append(_problem(path, f"secret field '{token}' cannot be interpolated"))


def _require_field(
    path: str,
    field_name: str,
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict[str, Any]],
    problems: list[dict[str, str]],
    *,
    secret: bool,
) -> None:
    field = fields_by_name.get(field_name)
    if field is None:
        problems.append(_problem(path, f"unknown field '{field_name}'"))
        return
    if field_name not in variant_fields:
        problems.append(
            _problem(path, f"field '{field_name}' is not part of variant '{variant_name}'")
        )
        return
    if secret and not field["secret"]:
        problems.append(_problem(path, f"field '{field_name}' must be a secret"))


@dataclass(frozen=True)
class _BoxCommand:
    package: str
    version: str


def _parse_box_command(text: str) -> _BoxCommand:
    if any(mark in text for mark in (";", "$(")):
        raise _Invalid([_problem("server.command", "box command contains a shell metacharacter")])
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise _Invalid([_problem("server.command", "box command could not be parsed")]) from exc
    if not tokens:
        raise _Invalid([_problem("server.command", "box command is empty")])
    runner = tokens[0]
    if runner == "uvx":
        return _parse_uvx(tokens[1:])
    if runner == "npx":
        return _parse_npx(tokens[1:])
    raise _Invalid([_problem("server.command", "box command must start with uvx or npx")])


def _parse_uvx(tokens: list[str]) -> _BoxCommand:
    if not tokens or tokens[0].startswith("-"):
        raise _Invalid([_problem("server.command", "uvx package must be name==version")])
    name, separator, version = tokens[0].partition("==")
    if separator != "==" or not name or not version or "==" in version:
        raise _Invalid([_problem("server.command", "uvx package must be name==version")])
    if any(mark in version for mark in ("*", "x", "X", "^", "~", ">", "<", ",")):
        raise _Invalid([_problem("server.command", f"version {version!r} is not an exact version")])
    try:
        Version(version)
    except InvalidVersion as exc:
        raise _Invalid(
            [_problem("server.command", f"version {version!r} is not an exact version")]
        ) from exc
    if not _PY_NAME_RE.fullmatch(name):
        raise _Invalid([_problem("server.command", "uvx package name is not valid")])
    return _BoxCommand(package=name, version=version)


def _parse_npx(tokens: list[str]) -> _BoxCommand:
    index = 0
    while index < len(tokens) and tokens[index] in _NPM_YES:
        index += 1
    if index >= len(tokens) or tokens[index].startswith("-"):
        raise _Invalid([_problem("server.command", "npx package must be name@version")])
    token = tokens[index]
    if token.startswith("@"):
        slash = token.find("/")
        at = token.find("@", slash + 1) if slash > 0 else -1
    else:
        at = token.rfind("@")
    if at <= 0 or at == len(token) - 1:
        raise _Invalid([_problem("server.command", "npx package must be name@version")])
    package, version = token[:at], token[at + 1 :]
    if not _NPM_NAME_RE.fullmatch(package):
        raise _Invalid([_problem("server.command", "npx package name is not valid")])
    if _NPM_VERSION_RE.fullmatch(version) is None:
        raise _Invalid(
            [_problem("server.command", f"version {version!r} is not an exact X.Y.Z version")]
        )
    return _BoxCommand(package=package, version=version)


def _package_identity(
    document: dict[str, Any], *, name: str | None, namespace: str | None
) -> tuple[str, str]:
    provider_name = str(document["name"])
    package = provider_name if name is None else name
    namespace_name = provider_name.replace("-", "_") if namespace is None else namespace
    problems: list[dict[str, str]] = []
    if _RESERVED_NAME_RE.search(package) or _PACKAGE_NAME_RE.fullmatch(package) is None:
        problems.append(_problem("name", f"package name '{package}' does not match the manifest"))
    if len(namespace_name) > 32 or _NAMESPACE_RE.fullmatch(namespace_name) is None:
        problems.append(
            _problem("namespace", f"namespace '{namespace_name}' does not match the manifest")
        )
    if problems:
        raise _Invalid(problems)
    return package, namespace_name


def _obtain_snapshot(
    directory: Path,
    document: dict[str, Any],
    *,
    image: str,
    tools_snapshot: Path | None,
    no_discovery: bool,
    runner: _Runner,
) -> tuple[dict[str, Any] | None, str]:
    if no_discovery:
        return None, "skipped"
    if tools_snapshot is not None:
        path = Path(tools_snapshot)
        if not path.is_file():
            raise _Invalid([_problem(str(path), "tools snapshot file not found")])
        text = path.read_text(encoding="utf-8")
        return _parse_snapshot(text, document), "file"
    if shutil.which("docker") is None:
        raise _Invalid(
            [
                {
                    "id": "docker_unavailable",
                    "message": "Docker is needed to ask the server for its tools.",
                    "hint": "Install Docker, or pass --tools-snapshot with a snapshot taken elsewhere.",
                    "retryable": False,
                    "severity": "error",
                }
            ]
        )
    args = _docker_args(directory, image, str(document["name"]))
    try:
        completed = _invoke(runner, args)
    except FileNotFoundError as exc:
        raise _Invalid(
            [
                {
                    "id": "docker_unavailable",
                    "message": "Docker is needed to ask the server for its tools.",
                    "hint": "Install Docker, or pass --tools-snapshot with a snapshot taken elsewhere.",
                    "retryable": False,
                    "severity": "error",
                }
            ]
        ) from exc
    if getattr(completed, "returncode", 1) != 0:
        stderr = _stderr_tail(str(getattr(completed, "stderr", "") or ""))
        raise _Invalid(
            [
                {
                    "id": "discovery_failed",
                    "message": ERRORS["discovery_failed"].title,
                    "hint": ERRORS["discovery_failed"].action,
                    "retryable": True,
                    "stderr": stderr,
                    "severity": "error",
                }
            ]
        )
    stdout = str(getattr(completed, "stdout", "") or "")
    try:
        json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _Invalid(
            [
                {
                    "id": "discovery_failed",
                    "message": ERRORS["discovery_failed"].title,
                    "hint": ERRORS["discovery_failed"].action,
                    "retryable": True,
                    "stderr": _stderr_tail(str(getattr(completed, "stderr", "") or "")),
                    "severity": "error",
                }
            ]
        ) from exc
    return _parse_snapshot(stdout, document), "docker"


def _invoke(runner: _Runner | Any, args: list[str], *, timeout: float = _DISCOVERY_TIMEOUT) -> Any:
    method = getattr(runner, "run", None)
    if callable(method):
        return method(args, timeout=timeout)
    return runner(args)


def _docker_args(directory: Path, image: str, name: str) -> list[str]:
    mounted = directory.resolve()
    container_dir = f"/opt/box/packages/{name}"
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "stdio-box",
        "-v",
        f"{mounted}:{container_dir}:ro",
        "-e",
        f"BOX_PROVIDER_DIR={container_dir}",
        image,
        "snapshot-tools",
        "--out",
        "-",
    ]


def _stderr_tail(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-_STDERR_TAIL:])


def _parse_snapshot(text: str, document: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Invalid([_problem("tools.snapshot.json", f"invalid JSON: {exc}")]) from exc
    problems: list[dict[str, str]] = []
    if not isinstance(loaded, dict):
        problems.append(_problem("tools.snapshot.json", "document must be an object"))
    else:
        if loaded.get("schema_version") != 1:
            problems.append(_problem("tools.snapshot.json", "schema_version must be 1"))
        server = loaded.get("server")
        command = server.get("command") if isinstance(server, dict) else None
        if not isinstance(command, str):
            problems.append(_problem("tools.snapshot.json", "server.command is required"))
        elif command != document["server"]["command"]:
            problems.append(
                _problem("tools.snapshot.json", "snapshot was taken from another server version")
            )
        taken_at = loaded.get("taken_at")
        if not isinstance(taken_at, str):
            problems.append(
                _problem("tools.snapshot.json", "taken_at must be an ISO-8601 timestamp")
            )
        else:
            try:
                datetime.fromisoformat(taken_at)
            except ValueError:
                problems.append(
                    _problem("tools.snapshot.json", "taken_at must be an ISO-8601 timestamp")
                )
        if not isinstance(loaded.get("tools"), list):
            problems.append(_problem("tools.snapshot.json", "tools must be a list"))
    if problems:
        raise _Invalid(problems)
    loaded["text"] = text
    return loaded


def _published_tools(snapshot: dict[str, Any], document: dict[str, Any]) -> list[_Published]:
    rules = document.get("tools") or {}
    hidden = set(rules.get("hide") or [])
    prefix = rules.get("strip_prefix")
    normalize = rules.get("normalize")
    rename = dict(rules.get("rename") or {})
    published: list[_Published] = []
    seen: dict[str, str] = {}
    problems: list[dict[str, str]] = []
    for index, raw in enumerate(snapshot["tools"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            problems.append(_problem(f"tools[{index}].name", "tool name must be a string"))
            continue
        child = raw["name"]
        name = _publish_one(child, hidden=hidden, prefix=prefix, normalize=normalize, rename=rename)
        if name is None:
            continue
        if name.startswith("creds_") or _PUBLISHED_NAME_RE.fullmatch(name) is None:
            problems.append(
                _problem(f"tools[{index}].name", f"published name '{name}' is not allowed")
            )
            continue
        if name in seen:
            problems.append(
                _problem(
                    f"tools[{index}].name",
                    f"duplicate published name '{name}'",
                )
            )
            continue
        seen[name] = child
        annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
        published.append(
            _Published(
                name=name,
                read_only=annotations.get("readOnlyHint") is True,
                destructive=annotations.get("destructiveHint") is True,
            )
        )
    if problems:
        raise _Invalid(problems)
    return published


def _publish_one(
    child: str,
    *,
    hidden: set[str],
    prefix: str | None,
    normalize: str | None,
    rename: dict[str, str],
) -> str | None:
    if child in hidden:
        return None
    name = child
    if prefix and name.startswith(prefix):
        name = name[len(prefix) :]
    if normalize == "snake":
        name = _SNAKE_RE.sub("_", name).strip("_").lower()
    name = rename.get(name, name)
    if name in hidden:
        return None
    return name


def _override_hints(
    published: list[_Published],
    *,
    read_only: Sequence[str] | None,
    destructive: Sequence[str] | None,
) -> list[_Published]:
    """``--read-only`` and ``--destructive`` replace the snapshot's hints, each as a whole set."""
    if read_only is None and destructive is None:
        return published
    known = {item.name for item in published}
    problems: list[dict[str, str]] = []
    for flag, names in (("--read-only", read_only), ("--destructive", destructive)):
        for item in names or ():
            if item not in known:
                problems.append(_problem(flag, f"'{item}' is not a published tool name"))
    both = sorted(set(read_only or ()) & set(destructive or ()))
    for item in both:
        problems.append(
            _problem("--read-only", f"'{item}' is in both --read-only and --destructive")
        )
    if problems:
        raise _Invalid(problems)
    chosen_read = None if read_only is None else set(read_only)
    chosen_destructive = None if destructive is None else set(destructive)
    return [
        _Published(
            name=item.name,
            read_only=item.read_only if chosen_read is None else item.name in chosen_read,
            destructive=(
                item.destructive if chosen_destructive is None else item.name in chosen_destructive
            ),
        )
        for item in published
    ]


def _box_tools(image: str, runner: _Runner) -> tuple[list[str], str]:
    """The image's own ``creds_*`` tools from ``stdio-box box-tools``, else the fallback list."""
    fallback = (list(CREDS_TOOLS), "fallback")
    if shutil.which("docker") is None:
        return fallback
    args = ["docker", "run", "--rm", "--entrypoint", "stdio-box", image, "box-tools"]
    try:
        completed = _invoke(runner, args, timeout=_BOX_TOOLS_TIMEOUT)
    except OSError:
        return fallback
    if getattr(completed, "returncode", 1) != 0:
        return fallback
    try:
        loaded = json.loads(str(getattr(completed, "stdout", "") or ""))
    except json.JSONDecodeError:
        return fallback
    if (
        not isinstance(loaded, list)
        or not loaded
        or not all(isinstance(item, str) and _BOX_TOOL_RE.fullmatch(item) for item in loaded)
        or len(set(loaded)) != len(loaded)
        # mcp.critical always names these; a list without them would not lint.
        or not set(CHANGING_CREDS) <= set(loaded)
    ):
        return fallback
    return list(loaded), "image"


def _empty_snapshot_text(document: dict[str, Any]) -> str:
    """A snapshot with no server tools, in the exact form ``stdio-box snapshot-tools`` writes."""
    snapshot = {
        "schema_version": 1,
        "server": {"command": str(document["server"]["command"])},
        "taken_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tools": [],
    }
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def _render_manifest(
    document: dict[str, Any],
    *,
    package: str,
    namespace: str,
    tools: list[str],
    critical: list[str],
    image: str,
    derived_image: bool,
    stock_image: bool,
    parsed: _BoxCommand,
    publisher: str | None,
    homepage: str | None,
    version: str | None,
) -> str:
    title = str(document["title"])
    summary = (
        f"{title} tools ({parsed.package} {parsed.version}) behind Keystone; "
        "each person, service or role uses its own account."
    )
    command = str(document["server"]["command"])
    provider_name = str(document["name"])
    lines = [
        "schema_version: 1",
        "kind: service",
        f"name: {package}",
        f"version: {version or _PACKAGE_VERSION}",
        f"publisher: {_yaml_plain(publisher or _PUBLISHER)}",
        f"summary: {json.dumps(summary)}",
        (
            f"homepage: {json.dumps(homepage)}"
            if homepage is not None
            else "homepage: "
            f"https://github.com/keystone-codes/stdio-box/tree/{package}/v1.0.0/packages/{package}"
        ),
        "compat:",
        '  keystone_mcp: ">=0.5.0,<2.0"',
        "  contracts: [upstream-registry/1, identity-assertion/1]",
        "mcp:",
        "  container: mcp",
        "  transport: http",
        "  path: /mcp",
        "  healthz: /healthz",
        f"  namespace: {namespace}",
        "  bearer_env: BOX_BEARER_TOKEN",
        "  tools:",
        *[f"    - {_yaml_plain(item)}" for item in tools],
        "  critical:",
        *[f"    - {_yaml_plain(item)}" for item in critical],
        "identity:",
        "  signed_assertion: required",
        "  secret_env: BOX_IDENTITY_SECRET",
        "  audience_env: BOX_IDENTITY_AUDIENCE",
        "containers:",
        "  mcp:",
    ]
    if derived_image:
        lines.append("    # placeholder until the release workflow pins the digest")
    lines.extend(
        [
            f"    image: {json.dumps(image)}",
            "    port: 8081",
        ]
    )
    if stock_image:
        lines.extend(_package_dir_lines("    "))
    lines.extend(
        [
            "    env:",
            "      - name: BOX_PROVIDER",
            f"        default: {json.dumps(provider_name)}",
            '        description: "Provider directory inside the stdio-box image."',
            "      - name: BOX_COMMAND",
            f"        default: {json.dumps(command)}",
            (
                "        description: "
                '"Server to run. The package and version must match the provider lock file."'
            ),
        ]
    )
    if stock_image:
        lines.extend(_provider_dir_env_lines("      "))
    lines.extend(
        [
            "    secrets:",
            "      - name: BOX_STORE_KEY",
            "        generate:",
            "          bytes: 32",
            "    volumes:",
            "      - name: store",
            "        path: /data",
            "      - name: cache",
            "        path: /cache",
            "    healthcheck:",
            "      path: /healthz",
            "      interval: 10s",
            "      start_period: 300s",
            "needs: []",
            "requires: []",
            "public_paths:",
            "  - /creds/",
            "public_base_url_env: BOX_PUBLIC_BASE_URL",
            "recommended_policy: policy.example.yaml",
            "",
        ]
    )
    return "\n".join(lines)


def _package_dir_lines(indent: str) -> list[str]:
    return [
        f"{indent}package_dir:",
        f'{indent}  from: "."',
        f"{indent}  path: {STOCK_PROVIDER_DIR}",
    ]


def _provider_dir_env_lines(indent: str) -> list[str]:
    return [
        f"{indent}- name: BOX_PROVIDER_DIR",
        f"{indent}  default: {json.dumps(STOCK_PROVIDER_DIR)}",
        (
            f"{indent}  description: "
            '"Package files mounted read-only: provider.yaml, the lock and tools.snapshot.json."'
        ),
    ]


def _yaml_plain(value: str) -> str:
    if value in _YAML_BOOL or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return json.dumps(value)
    return value


def _render_policy(
    document: dict[str, Any],
    *,
    namespace: str,
    published: list[_Published],
) -> str:
    subject = _policy_subject(str(document["title"]))
    read_only = [f"{namespace}_{item.name}" for item in published if item.read_only]
    server_names = [f"{namespace}_{item.name}" for item in published]
    destructive = [f"{namespace}_{item.name}" for item in published if item.destructive]
    reader = [*read_only, *[f"{namespace}_{name}" for name in _POLICY_CREDS]]
    writer = [*server_names, *[f"{namespace}_{name}" for name in _POLICY_CREDS]]
    changing = [f"{namespace}_{name}" for name in CHANGING_CREDS]
    critical = [*destructive, *changing]
    lines = [
        "# Example only. keystonectl prints this file and never merges it into your policy.",
        (
            "# The box does not check roles: whoever the policy lets call "
            f"{namespace}_creds_* manages"
        ),
        (
            "# everyone's accounts in this box. Do not give a non-administrator role "
            f"{namespace}_* :"
        ),
        f"# it also matches {namespace}_creds_*.",
        'version: "2.0.0"',
        "default_deny: true",
        "roles:",
        f"  {namespace}_reader:",
        f"    description: {json.dumps(f'Read {subject} through the {namespace} box.')}",
        "    allow:",
        *[f"      - {_yaml_plain(item)}" for item in reader],
        f"  {namespace}_writer:",
        (
            "    description: "
            f"{json.dumps(f'Read and change {subject} through the {namespace} box.')}"
        ),
        "    allow:",
        *[f"      - {_yaml_plain(item)}" for item in writer],
        f"  {namespace}_box_admin:",
        (f"    description: {json.dumps(f'Manage accounts and bindings in the {namespace} box.')}"),
        "    allow:",
        f"      - {json.dumps(f'{namespace}_creds_*')}",
        "critical_tools:",
        *[f"  - {_yaml_plain(item)}" for item in critical],
        ("# Only a signed-in person may change accounts; API keys and service accounts may not."),
        "requires_human_identity:",
        *[f"  - {_yaml_plain(item)}" for item in changing],
        "",
    ]
    return "\n".join(lines)


def _policy_subject(title: str) -> str:
    match = re.fullmatch(r".+ \((.+)\)", title.strip())
    if match:
        return match.group(1)
    return title.strip()


def _update_manifest_text(
    text: str,
    *,
    tools: list[str],
    critical: list[str],
    command: str,
    stock_image: bool = False,
    publisher: str | None = None,
    homepage: str | None = None,
    version: str | None = None,
) -> str:
    lines = text.splitlines(keepends=True)
    if stock_image:
        lines = _ensure_stock_image(lines)
    lines = _replace_named_list(lines, "critical", critical)
    lines = _replace_named_list(lines, "tools", tools)
    lines = _replace_command("".join(lines), command).splitlines(keepends=True)
    if version is not None:
        lines = _set_top_level(lines, "version", version)
    if publisher is not None:
        lines = _set_top_level(lines, "publisher", _yaml_plain(publisher))
    if homepage is not None:
        lines = _set_top_level(lines, "homepage", json.dumps(homepage), after="publisher")
    return "".join(lines)


def _indent_of(line: str) -> int:
    raw = line.rstrip("\r\n")
    return len(raw) - len(raw.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _newline(lines: list[str]) -> str:
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"


def _top_level_key(lines: list[str], key: str) -> int | None:
    for index, line in enumerate(lines):
        if _indent_of(line) == 0 and _mapping_key(line) == key:
            return index
    return None


def _child_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    """Index of ``key:`` among the direct children in ``lines[start:end]``."""
    child_indent = None
    for index in range(start, end):
        if not _is_content(lines[index]):
            continue
        current = _indent_of(lines[index])
        if child_indent is None:
            child_indent = current
        if current == child_indent and _mapping_key(lines[index]) == key:
            return index
    return None


def _block_end(lines: list[str], index: int) -> int:
    """End of the block under the key at ``index``, without trailing blank or comment lines."""
    base = _indent_of(lines[index])
    end = len(lines)
    for cursor in range(index + 1, len(lines)):
        if not _is_content(lines[cursor]):
            continue
        current = _indent_of(lines[cursor])
        if current < base or (current == base and not lines[cursor].lstrip().startswith("- ")):
            end = cursor
            break
    while end > index + 1 and not _is_content(lines[end - 1]):
        end -= 1
    return end


def _key_has_inline_value(line: str) -> bool:
    _key, _sep, rest = line.strip().partition(":")
    rest = rest.strip()
    return bool(rest) and not rest.startswith("#")


def _insert(lines: list[str], at: int, new: list[str]) -> list[str]:
    newline = _newline(lines)
    head = list(lines[:at])
    if head and not head[-1].endswith("\n"):
        head[-1] += newline
    return [*head, *(item + newline for item in new), *lines[at:]]


def _ensure_stock_image(lines: list[str]) -> list[str]:
    """Add ``package_dir`` and ``BOX_PROVIDER_DIR`` to ``containers.mcp`` when they are missing."""
    try:
        document = yaml.safe_load("".join(lines))
    except yaml.YAMLError as exc:
        raise _Unrecognized("containers.mcp") from exc
    containers = document.get("containers") if isinstance(document, dict) else None
    container = containers.get("mcp") if isinstance(containers, dict) else None
    top = _top_level_key(lines, "containers")
    if not isinstance(container, dict) or top is None:
        raise _Unrecognized("containers.mcp")
    role = _child_key(lines, top + 1, _block_end(lines, top), "mcp")
    if role is None or _key_has_inline_value(lines[role]):
        raise _Unrecognized("containers.mcp")
    role_end = _block_end(lines, role)
    first = next((i for i in range(role + 1, role_end) if _is_content(lines[i])), None)
    if first is None:
        raise _Unrecognized("containers.mcp")
    indent = " " * _indent_of(lines[first])

    if "package_dir" not in container:
        env_key = _child_key(lines, role + 1, role_end, "env")
        at = role_end if env_key is None else env_key
        lines = _insert(lines, at, _package_dir_lines(indent))
        role_end = _block_end(lines, role)

    env = container.get("env")
    names = (
        {item.get("name") for item in env if isinstance(item, dict)}
        if isinstance(env, list)
        else set()
    )
    if "BOX_PROVIDER_DIR" in names:
        return lines
    env_key = _child_key(lines, role + 1, role_end, "env")
    if env_key is None:
        if env is not None:
            raise _Unrecognized("containers.mcp.env")
        return _insert(lines, role_end, [f"{indent}env:", *_provider_dir_env_lines(indent + "  ")])
    if _key_has_inline_value(lines[env_key]):
        raise _Unrecognized("containers.mcp.env")
    env_end = _block_end(lines, env_key)
    item = next(
        (i for i in range(env_key + 1, env_end) if lines[i].lstrip().startswith("- ")),
        None,
    )
    item_indent = indent + "  " if item is None else " " * _indent_of(lines[item])
    return _insert(lines, env_end, _provider_dir_env_lines(item_indent))


def _set_top_level(
    lines: list[str], key: str, value: str, *, after: str | None = None
) -> list[str]:
    index = _top_level_key(lines, key)
    if index is None:
        anchor = None if after is None else _top_level_key(lines, after)
        if anchor is None:
            raise _Unrecognized(key)
        return _insert(lines, anchor + 1, [f"{key}: {value}"])
    following = next((i for i in range(index + 1, len(lines)) if _is_content(lines[i])), None)
    if following is not None and _indent_of(lines[following]) > 0:
        raise _Unrecognized(key)
    line = lines[index]
    ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
    _key, _sep, rest = line[: len(line) - len(ending)].partition(":")
    rest = rest.strip()
    if rest.startswith(("|", ">", "&", "*", "!")):
        raise _Unrecognized(key)
    comment = f" {rest}" if rest.startswith("#") else _trailing_comment(rest)
    lines = list(lines)
    lines[index] = f"{key}: {value}{comment}{ending}"
    return lines


def _replace_named_list(lines: list[str], key: str, items: list[str]) -> list[str]:
    section = _mapping_section(lines, "mcp")
    if section is None:
        raise _Unrecognized("mcp")
    start, _indent, end = section
    found = None
    for index in range(start + 1, end):
        if _mapping_key(lines[index]) == key:
            found = index
            break
    if found is None:
        raise _Unrecognized(f"mcp.{key}")
    key_line = lines[found].rstrip("\r\n")
    key_indent = len(key_line) - len(key_line.lstrip(" "))
    _key, _sep, rest = key_line.strip().partition(":")
    if rest.strip() and not rest.strip().startswith("#"):
        raise _Unrecognized(f"mcp.{key}")
    body_start = found + 1
    body_end = body_start
    item_indent = None
    while body_end < end:
        raw = lines[body_end].rstrip("\r\n")
        if not raw.strip():
            body_end += 1
            continue
        if raw.lstrip().startswith("#") and (len(raw) - len(raw.lstrip(" "))) > key_indent:
            body_end += 1
            continue
        current = len(raw) - len(raw.lstrip(" "))
        if current <= key_indent:
            break
        if raw.lstrip().startswith("- "):
            item_indent = current
            body_end += 1
            continue
        raise _Unrecognized(f"mcp.{key}")
    while body_end > body_start and not lines[body_end - 1].strip():
        body_end -= 1
    if item_indent is None:
        item_indent = key_indent + 2
    newline = "\n"
    if lines[found].endswith("\r\n"):
        newline = "\r\n"
    rendered = [f"{' ' * item_indent}- {_yaml_plain(item)}{newline}" for item in items]
    return [*lines[:body_start], *rendered, *lines[body_end:]]


def _mapping_section(lines: list[str], key: str) -> tuple[int, int, int] | None:
    for index, line in enumerate(lines):
        if _mapping_key(line) != key:
            continue
        raw = line.rstrip("\r\n")
        indent = len(raw) - len(raw.lstrip(" "))
        end = len(lines)
        for cursor in range(index + 1, len(lines)):
            current = lines[cursor].rstrip("\r\n")
            if not current.strip() or current.lstrip().startswith("#"):
                continue
            if len(current) - len(current.lstrip(" ")) <= indent:
                end = cursor
                break
        return index, indent, end
    return None


def _mapping_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("-", "#")):
        return None
    key, sep, _rest = stripped.partition(":")
    if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return None
    return key


def _replace_command(text: str, command: str) -> str:
    lines = text.splitlines(keepends=True)
    quoted = json.dumps(command)
    for index, line in enumerate(lines):
        if "BOX_COMMAND" not in line:
            continue
        if re.search(r"default\s*:", line):
            lines[index] = _swap_default(line, quoted)
            return "".join(lines)
        base = line.rstrip("\r\n")
        base_indent = len(base) - len(base.lstrip(" "))
        for cursor in range(index + 1, len(lines)):
            raw = lines[cursor].rstrip("\r\n")
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= base_indent:
                break
            if re.match(r"\s*default\s*:", raw):
                lines[cursor] = _swap_default(lines[cursor], quoted)
                return "".join(lines)
        raise _Unrecognized("BOX_COMMAND")
    raise _Unrecognized("BOX_COMMAND")


def _swap_default(line: str, quoted: str) -> str:
    ending = ""
    body = line
    if body.endswith("\r\n"):
        ending = "\r\n"
        body = body[:-2]
    elif body.endswith("\n"):
        ending = "\n"
        body = body[:-1]
    match = re.match(r"^(\s*default\s*:\s*)(.*)$", body)
    if match:
        return f"{match.group(1)}{quoted}{_trailing_comment(match.group(2))}{ending}"
    flow = re.search(r"(default\s*:\s*)(\"(?:\\.|[^\"\\])*\"|'(?:[^']*)'|\S+)", body)
    if flow is None:
        raise _Unrecognized("BOX_COMMAND")
    return body[: flow.start(2)] + quoted + body[flow.end(2) :] + ending


def _trailing_comment(rest: str) -> str:
    if rest.startswith('"'):
        index = 1
        while index < len(rest):
            if rest[index] == "\\":
                index += 2
                continue
            if rest[index] == '"':
                return rest[index + 1 :]
            index += 1
        return ""
    if rest.startswith("'"):
        end = rest.find("'", 1)
        return "" if end < 0 else rest[end + 1 :]
    marker = rest.find(" #")
    return "" if marker < 0 else rest[marker:]


def _write_text(path: Path, text: str) -> None:
    atomic_write(path, text.encode("utf-8"), mode=0o644)
