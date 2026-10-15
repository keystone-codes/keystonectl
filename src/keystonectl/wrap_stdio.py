"""Write a stdio-box package from a provider directory."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
) -> Result:
    """Validate a provider directory and write its box package files."""
    command = "package wrap-stdio"
    directory = Path(provider_dir)
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
    except _Invalid as exc:
        return _failed(command, directory, "provider_invalid", problems=exc.problems)

    tool_names = [item.name for item in published] + list(CREDS_TOOLS)
    destructive = [item.name for item in published if item.destructive]
    critical = [*destructive, *CHANGING_CREDS]
    warnings = []
    if discovery == "skipped":
        spec = WARNINGS["tools_snapshot_missing"]
        warnings.append({"id": spec.id, "message": spec.title})

    files: list[str] = []
    if snapshot is not None:
        _write_text(directory / "tools.snapshot.json", snapshot["text"])
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
                    parsed=parsed,
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
            "warnings": warnings,
        },
    )


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
    args = _docker_args(directory, image)
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


def _invoke(runner: _Runner | Any, args: list[str]) -> Any:
    method = getattr(runner, "run", None)
    if callable(method):
        return method(args, timeout=_DISCOVERY_TIMEOUT)
    return runner(args)


def _docker_args(directory: Path, image: str) -> list[str]:
    mounted = directory.resolve()
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "stdio-box",
        "-v",
        f"{mounted}:/opt/box/provider:ro",
        "-e",
        "BOX_PROVIDER_DIR=/opt/box/provider",
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


def _render_manifest(
    document: dict[str, Any],
    *,
    package: str,
    namespace: str,
    tools: list[str],
    critical: list[str],
    image: str,
    derived_image: bool,
    parsed: _BoxCommand,
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
        f"version: {_PACKAGE_VERSION}",
        f"publisher: {_PUBLISHER}",
        f"summary: {json.dumps(summary)}",
        (
            "homepage: "
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


def _update_manifest_text(text: str, *, tools: list[str], critical: list[str], command: str) -> str:
    lines = text.splitlines(keepends=True)
    lines = _replace_named_list(lines, "critical", critical)
    lines = _replace_named_list(lines, "tools", tools)
    return _replace_command("".join(lines), command)


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
