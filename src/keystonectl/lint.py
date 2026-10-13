"""``keystonectl lint`` for a package manifest or a catalog file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from keystonectl.catalog import CatalogError, catalog_errors, load_catalog
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.keystone_mcp_rules import BUILTIN_TOOL_NAMES_VERSION
from keystonectl.manifest import ManifestError, manifest_errors
from keystonectl.output import Result

_PATH_RE = re.compile(
    r"^\$?(?:[A-Za-z_][A-Za-z0-9_-]*|\[[0-9]+\])(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|\[[0-9]+\]))*$"
)


def _problem(path: str, message: str, severity: str) -> dict[str, str]:
    return {"path": path, "message": message, "severity": severity}


def _from_manifest_line(line: str) -> dict[str, str]:
    head, separator, _tail = line.partition(": ")
    if separator and " " not in head and (_PATH_RE.match(head) or head.startswith("containers.")):
        return _problem(head, line, "error")
    return _problem("$", line, "error")


def _warning(warning_id: str, message: str) -> dict[str, str]:
    return {"id": warning_id, "message": message}


def locate(target: Path) -> tuple[str, Path, Path]:
    """Return ``(kind, file, directory)`` for a manifest or catalog argument."""
    target = Path(target)
    if target.is_dir():
        manifest = target / "keystone-service.yaml"
        catalog = target / "catalog.yaml"
        if manifest.is_file():
            return "manifest", manifest, target
        if catalog.is_file():
            return "catalog", catalog, target
        raise ManifestError([f"{target}: no keystone-service.yaml or catalog.yaml"])
    if target.name == "catalog.yaml":
        return "catalog", target, target.parent
    return "manifest", target, target.parent


def _published_name_warnings(document: dict[str, Any]) -> list[dict[str, str]]:
    mcp = document.get("mcp")
    if not isinstance(mcp, dict):
        return []
    namespace = mcp.get("namespace")
    tools = mcp.get("tools")
    if not isinstance(namespace, str) or not isinstance(tools, list):
        return []
    warnings: list[dict[str, str]] = []
    for tool in tools:
        if not isinstance(tool, str):
            continue
        published = f"{namespace}_{tool}"
        if len(published) > 64:
            warnings.append(
                _warning(
                    "published_name_long",
                    f"{published} is longer than 64 characters. "
                    "Many MCP clients reject names that long.",
                )
            )
    return warnings


def _file_problems(document: dict[str, Any], directory: Path) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    policy = document.get("recommended_policy")
    if isinstance(policy, str):
        path = directory / policy
        if not path.is_file():
            problems.append(
                _problem(
                    "recommended_policy", f"recommended_policy: {policy} does not exist", "error"
                )
            )
    provision = document.get("provision")
    if isinstance(provision, dict) and isinstance(provision.get("script"), str):
        script = provision["script"]
        if not (directory / script).is_file():
            problems.append(
                _problem("provision.script", f"provision.script: {script} does not exist", "error")
            )
    return problems


def _compose_warning(directory: Path) -> dict[str, str] | None:
    for name in ("compose.yaml", "docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        if (directory / name).is_file():
            return _warning(
                "compose_file_ignored",
                f"{name} is present. The installer never reads it.",
            )
    return None


def _stale_warning(version: str | None) -> dict[str, str] | None:
    if not version:
        return None
    try:
        newer = Version(version) > Version(BUILTIN_TOOL_NAMES_VERSION)
    except InvalidVersion:
        return None
    if not newer:
        return None
    spec = WARNINGS["builtin_list_may_be_stale"]
    return _warning("builtin_list_may_be_stale", spec.action)


def lint_manifest(
    path: Path,
    directory: Path,
    *,
    keystone_version: str | None,
    check_files: bool = True,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [_problem(str(path), f"{path}: {exc.strerror or exc}", "error")], []
    except yaml.YAMLError as exc:
        return [_problem(str(path), f"{path}: invalid YAML: {exc}", "error")], []
    if not isinstance(document, dict):
        return [_problem("$", "$: must be an object", "error")], []
    problems = [_from_manifest_line(line) for line in manifest_errors(document)]
    warnings = _published_name_warnings(document)
    if check_files:
        problems.extend(_file_problems(document, directory))
        compose = _compose_warning(directory)
        if compose:
            warnings.append(compose)
    stale = _stale_warning(keystone_version)
    if stale:
        warnings.append(stale)
    for item in warnings:
        problems.append(_problem(item["id"], item["message"], "warning"))
    return problems, warnings


def lint_catalog(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    try:
        document = load_catalog(path)
    except CatalogError as exc:
        problems = []
        for message in exc.messages:
            head, separator, _tail = message.partition(": ")
            path_text = head if separator else "$"
            problems.append(_problem(path_text, message, "error"))
        if not problems:
            problems.append(_problem("$", "catalog is invalid", "error"))
        return problems, []
    extra = catalog_errors(document)
    return ([_problem("$", message, "error") for message in extra], [])


def render_text(problems: list[dict[str, str]]) -> str:
    errors = [item for item in problems if item["severity"] == "error"]
    warnings = [item for item in problems if item["severity"] == "warning"]
    lines = [f"{item['severity']} {item['path']}: {item['message']}" for item in errors + warnings]
    lines.append(f"{_count(len(errors), 'error')}, {_count(len(warnings), 'warning')}")
    return "\n".join(lines)


def _count(count: int, word: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {word}{suffix}"


def run_lint(target: Path, *, keystone_version: str | None) -> Result:
    command = "lint"
    try:
        kind, path, directory = locate(target)
    except ManifestError as exc:
        spec = ERRORS["manifest_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=str(target),
            error={"id": spec.id, "message": exc.messages[0], "hint": spec.action},
            result={"problems": [_problem(str(target), exc.messages[0], "error")]},
        )
    if kind == "catalog":
        problems, warnings = lint_catalog(path)
        error_id = "catalog_invalid"
    else:
        check_files = target.is_dir() or path.name == "keystone-service.yaml"
        problems, warnings = lint_manifest(
            path,
            directory,
            keystone_version=keystone_version,
            check_files=check_files,
        )
        error_id = "manifest_invalid"
    failed = any(item["severity"] == "error" for item in problems)
    if failed:
        spec = ERRORS[error_id]
        return Result(
            code=spec.code,
            command=command,
            summary=render_text(problems).splitlines()[-1],
            target=str(target),
            warnings=warnings,
            error={"id": spec.id, "message": spec.title, "hint": spec.action},
            result={"problems": problems, "text": render_text(problems)},
        )
    summary = render_text(problems).splitlines()[-1]
    return Result(
        code=0,
        command=command,
        summary=summary,
        target=str(target),
        warnings=warnings,
        result={"problems": problems, "text": render_text(problems)},
    )
