"""bundle plan and bundle install. Order follows requires; install reuses service apply."""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from keystonectl.catalog import CatalogError, load_sources
from keystonectl.compat import detect_keystone_mcp_version
from keystonectl.errors import ERRORS
from keystonectl.fetch import FetchError, describe_fetch_error, fetch_tree
from keystonectl.lock import load_lock
from keystonectl.output import Result, SecretRegistry, missing_summary
from keystonectl.plan import run_service
from keystonectl.runner import CommandRunner
from keystonectl.sources import (
    installed_service,
    package_cache,
    refresh_all,
    resolve_package,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Service-subparser hook. Bundle commands are registered on the root parser."""


def add_bundle_parser(subparsers: argparse._SubParsersAction) -> None:
    bundle = subparsers.add_parser("bundle", help="Plan or install a catalog bundle")
    bundle_sub = bundle.add_subparsers(dest="bundle_command")
    for name, help_text in (
        ("plan", "Show bundle order and the first missing answers. Writes nothing."),
        ("install", "Install bundle packages in requires order."),
    ):
        parser = bundle_sub.add_parser(name, help=help_text)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--root", default=None)
        parser.add_argument("--non-interactive", action="store_true")
        parser.add_argument("--set", action="append", default=None)
        parser.add_argument("--version", action="store_true")
        parser.add_argument("--defer-public", action="store_true")
        parser.add_argument("--certbot-staging", action="store_true")
        parser.add_argument("ref")


def install_bundle(
    root: Path,
    ref: str,
    *,
    plan_only: bool,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: dict[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock: Callable[[], datetime] | None = None,
    deployer: Any = None,
    defer_public: bool = False,
    certbot_staging: bool = False,
) -> Result:
    command = "bundle plan" if plan_only else "bundle install"
    try:
        sources = load_sources(root / "sources.yaml")
    except CatalogError as exc:
        spec = ERRORS["source_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=ref,
            error={"id": spec.id, "message": "; ".join(exc.messages), "hint": spec.action},
        )
    catalogs, warnings, error_id, message = refresh_all(root, sources, runner)
    if error_id:
        spec = ERRORS[error_id]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=ref,
            warnings=warnings,
            error={"id": spec.id, "message": message or spec.title, "hint": spec.action},
            retryable=spec.retryable,
        )
    lock = load_lock(root / "services.lock.yaml")
    detection = detect_keystone_mcp_version(root, lock=lock, environ=environ, answers=sets)
    resolved = _resolve_bundle(ref, sources, catalogs)
    if isinstance(resolved, Result):
        resolved.command = command
        resolved.target = ref
        resolved.warnings = [*warnings, *resolved.warnings]
        return resolved
    source_name, bundle = resolved
    source = next(item for item in sources if item.name == source_name)
    catalog = catalogs[source_name]
    requires, problem = _requires_map(
        root,
        source,
        catalog,
        list(bundle.get("packages") or []),
        lock,
        runner,
        keystone_version=detection.version,
    )
    if problem is not None:
        problem.command = command
        problem.target = ref
        problem.warnings = [*warnings, *problem.warnings]
        return problem
    names = [str(item) for item in bundle.get("packages") or []]
    cycle = _cycle(names, requires)
    if cycle:
        spec = ERRORS["requires_cycle"]
        shown = " -> ".join(cycle)
        return Result(
            code=spec.code,
            command=command,
            summary=f"requires cycle: {shown}",
            target=ref,
            warnings=warnings,
            error={
                "id": spec.id,
                "message": f"requires cycle: {shown}",
                "hint": spec.action,
            },
        )
    order = _toposort(names, requires)
    statuses = [{"name": name, "status": "not_started"} for name in order]
    policy_rows: list[dict[str, Any]] = []
    critical_rows: list[dict[str, Any]] = []
    changes = 0
    moment = clock or (lambda: datetime.now(UTC))
    for index, name in enumerate(order):
        satisfied = [
            item["name"]
            for item in statuses
            if item["status"] in {"planned", "installed", "unchanged"}
        ]
        outcome = run_service(
            root,
            "plan" if plan_only else "apply",
            f"{source_name}/{name}",
            runner=runner,
            secrets=secrets,
            sets=sets,
            environ=environ,
            interactive=interactive,
            clock=moment,
            deployer=deployer,
            defer_public=defer_public,
            certbot_staging=certbot_staging,
            assume_installed=satisfied,
        )
        if outcome.code == 2:
            statuses[index]["status"] = "needs_answers"
            return _stopped(
                command,
                ref,
                outcome,
                order,
                statuses,
                warnings,
                policy_rows,
                critical_rows,
                changes,
            )
        if outcome.code != 0:
            statuses[index]["status"] = "failed"
            return _stopped(
                command,
                ref,
                outcome,
                order,
                statuses,
                warnings,
                policy_rows,
                critical_rows,
                changes,
            )
        package_changes = outcome.changes
        if package_changes is None:
            package_changes = 0 if _unchanged(outcome) else 1
        if package_changes == 0:
            statuses[index]["status"] = "unchanged"
        else:
            statuses[index]["status"] = "planned" if plan_only else "installed"
            changes += package_changes
        policy_rows.extend(_policy_from(root, source_name, name, outcome))
        critical_rows.extend(_critical_from(root, source_name, name, outcome))
    return Result(
        code=0,
        command=command,
        summary=f"{'Planned' if plan_only else 'Installed'} {ref}.",
        target=ref,
        warnings=warnings,
        changes=changes,
        result=_body(order, statuses, policy_rows, critical_rows),
    )


def _resolve_bundle(
    ref: str, sources: list[Any], catalogs: Mapping[str, Mapping[str, Any]]
) -> tuple[str, dict[str, Any]] | Result:
    source_name, bundle_name = _parse_ref(ref)
    if bundle_name is None:
        spec = ERRORS["bundle_not_found"]
        return Result(
            code=spec.code,
            command="bundle",
            summary=spec.title,
            error={
                "id": spec.id,
                "message": f"Invalid bundle reference '{ref}'.",
                "hint": spec.action,
            },
        )
    found: list[tuple[str, dict[str, Any]]] = []
    for source in sources:
        if source_name and source.name != source_name:
            continue
        catalog = catalogs.get(source.name)
        if not isinstance(catalog, dict):
            continue
        bundles = catalog.get("bundles") if isinstance(catalog.get("bundles"), dict) else {}
        bundle = bundles.get(bundle_name)
        if isinstance(bundle, dict):
            found.append((source.name, bundle))
    if source_name and not found:
        spec = ERRORS["bundle_not_found"]
        return Result(
            code=spec.code,
            command="bundle",
            summary=spec.title,
            error={
                "id": spec.id,
                "message": f"Bundle {ref} was not found.",
                "hint": spec.action,
            },
        )
    if not found:
        spec = ERRORS["bundle_not_found"]
        return Result(
            code=spec.code,
            command="bundle",
            summary=spec.title,
            error={
                "id": spec.id,
                "message": f"Bundle {bundle_name} was not found.",
                "hint": spec.action,
            },
        )
    if len(found) > 1:
        choices = [f"{name}/{bundle_name}" for name, _bundle in found]
        return Result(
            code=2,
            command="bundle",
            summary=missing_summary(1),
            missing=[
                {
                    "var": "bundle",
                    "question": "Which bundle? The name is in more than one catalog.",
                    "kind": "argument",
                    "choices": choices,
                    "default": None,
                    "secret": False,
                }
            ],
        )
    return found[0]


def _parse_ref(ref: str) -> tuple[str | None, str | None]:
    text = ref.strip()
    if "/" in text:
        source, _slash, name = text.partition("/")
        if source and name and "/" not in name and "@" not in name:
            return source, name
        return None, None
    if text and "@" not in text and " " not in text:
        return None, text
    return None, None


def _requires_map(
    root: Path,
    source: Any,
    catalog: Mapping[str, Any],
    names: list[str],
    lock: Mapping[str, Any],
    runner: CommandRunner,
    *,
    keystone_version: str | None,
) -> tuple[dict[str, list[str]], Result | None]:
    requires: dict[str, list[str]] = {}
    bundle_names = set(names)
    installed = {
        str(item.get("name"))
        for item in (lock.get("services") or [])
        if isinstance(item, dict) and item.get("name")
    }
    for name in names:
        resolution = resolve_package(
            f"{source.name}/{name}",
            sources=[source],
            catalogs={source.name: dict(catalog)},
            lock=lock,
            keystone_version=keystone_version,
        )
        if resolution.error_id:
            spec = ERRORS[resolution.error_id]
            return {}, Result(
                code=spec.code,
                command="bundle",
                summary=resolution.message or spec.title,
                missing=resolution.missing,
                error=None
                if resolution.missing and spec.code == 2
                else {
                    "id": spec.id,
                    "message": resolution.message or spec.title,
                    "hint": resolution.hint or spec.action,
                },
                result=resolution.result,
            )
        package = resolution.package
        assert package is not None
        manifest = _read_manifest(root, package, runner)
        if isinstance(manifest, Result):
            return {}, manifest
        needed = [str(item) for item in (manifest.get("requires") or []) if isinstance(item, str)]
        missing = [item for item in needed if item not in bundle_names and item not in installed]
        if missing and installed_service(lock, missing[0]) is None:
            spec = ERRORS["requires_missing"]
            return {}, Result(
                code=spec.code,
                command="bundle",
                summary=f"Missing required packages: {', '.join(missing)}.",
                error={
                    "id": spec.id,
                    "message": f"Missing required packages: {', '.join(missing)}.",
                    "hint": spec.action,
                },
                result={"requires": missing},
            )
        requires[name] = needed
    return requires, None


def _read_manifest(root: Path, package: Any, runner: CommandRunner) -> dict[str, Any] | Result:
    dest = package_cache(root, package.source_name, package.name, package.version)
    path = _manifest_file(dest, package.path)
    tree: Path | None = None
    if not path.is_file():
        try:
            tree, _commit = fetch_tree(runner, package.git_url, package.ref)
        except FetchError as exc:
            spec = ERRORS[exc.error_id]
            message = describe_fetch_error(f"Package {package.name}", exc)
            return Result(
                code=spec.code,
                command="bundle",
                summary=spec.title,
                error={"id": spec.id, "message": message, "hint": spec.action},
                retryable=spec.retryable,
            )
        path = _manifest_file(tree, package.path)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        spec = ERRORS["manifest_invalid"]
        return Result(
            code=spec.code,
            command="bundle",
            summary=spec.title,
            error={"id": spec.id, "message": str(exc), "hint": spec.action},
        )
    if not isinstance(loaded, dict):
        spec = ERRORS["manifest_invalid"]
        return Result(
            code=spec.code,
            command="bundle",
            summary=spec.title,
            error={
                "id": spec.id,
                "message": f"{package.name} manifest is not a mapping.",
                "hint": spec.action,
            },
        )
    if tree is not None:
        shutil.rmtree(tree, ignore_errors=True)
    return loaded


def _manifest_file(base: Path, package_path: str) -> Path:
    if package_path in {"", "."}:
        return base / "keystone-service.yaml"
    return base / package_path / "keystone-service.yaml"


def _cycle(names: list[str], requires: Mapping[str, list[str]]) -> list[str] | None:
    present = set(names)
    graph = {name: [item for item in requires.get(name, []) if item in present] for name in names}
    color = {name: 0 for name in names}
    stack: list[str] = []

    def walk(name: str) -> list[str] | None:
        color[name] = 1
        stack.append(name)
        for dep in graph[name]:
            if color[dep] == 1:
                start = stack.index(dep)
                return stack[start:] + [dep]
            if color[dep] == 0:
                found = walk(dep)
                if found:
                    return found
        stack.pop()
        color[name] = 2
        return None

    for name in names:
        if color[name] == 0:
            found = walk(name)
            if found:
                return found
    return None


def _toposort(names: list[str], requires: Mapping[str, list[str]]) -> list[str]:
    """Stable order: dependencies first, otherwise the bundle list order."""

    present = set(names)
    index = {name: position for position, name in enumerate(names)}
    incoming = {name: 0 for name in names}
    after: dict[str, list[str]] = {name: [] for name in names}
    for name in names:
        for dep in requires.get(name, []):
            if dep in present:
                after[dep].append(name)
                incoming[name] += 1
    ready = sorted(
        [name for name, count in incoming.items() if count == 0], key=lambda item: index[item]
    )
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for nxt in after[name]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda item: index[item])
    return ordered


def _unchanged(outcome: Result) -> bool:
    if outcome.changes == 0:
        return True
    return bool(outcome.actions) and all(
        item.get("status") == "unchanged" for item in outcome.actions
    )


def _stopped(
    command: str,
    ref: str,
    outcome: Result,
    order: list[str],
    statuses: list[dict[str, str]],
    warnings: list[dict[str, str]],
    policy_rows: list[dict[str, Any]],
    critical_rows: list[dict[str, Any]],
    changes: int,
) -> Result:
    outcome.command = command
    outcome.target = ref
    outcome.warnings = [*warnings, *outcome.warnings]
    outcome.changes = changes
    body = _body(order, statuses, policy_rows, critical_rows)
    body.update(outcome.result)
    # Package statuses from this walk win over anything the inner command stored.
    body["order"] = order
    body["packages"] = statuses
    outcome.result = body
    return outcome


def _body(
    order: list[str],
    statuses: list[dict[str, str]],
    policy_rows: list[dict[str, Any]],
    critical_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {"order": order, "packages": statuses}
    if policy_rows:
        body["recommended_policy"] = policy_rows
    if critical_rows:
        body["critical_tools"] = critical_rows
    return body


def _manifest_cached(root: Path, source: str, name: str) -> dict[str, Any] | None:
    base = root / ".keystonectl" / "packages" / source / name
    if not base.is_dir():
        return None
    versions = sorted(path for path in base.iterdir() if path.is_dir())
    if not versions:
        return None
    path = versions[-1] / "keystone-service.yaml"
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _policy_from(root: Path, source: str, name: str, outcome: Result) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    blob = outcome.result.get("recommended_policy")
    if isinstance(blob, dict):
        row = {"package": name, "path": str(blob.get("path") or "")}
        if isinstance(blob.get("content"), str):
            row["content"] = blob["content"]
        rows.append(row)
        return rows
    manifest = _manifest_cached(root, source, name)
    if not isinstance(manifest, dict):
        return rows
    relative = manifest.get("recommended_policy")
    if isinstance(relative, str) and relative:
        rows.append({"package": name, "path": relative})
    return rows


def _critical_from(root: Path, source: str, name: str, outcome: Result) -> list[dict[str, Any]]:
    tools = outcome.result.get("critical_tools")
    if isinstance(tools, list) and tools:
        return [{"package": name, "name": str(item)} for item in tools]
    manifest = _manifest_cached(root, source, name)
    if not isinstance(manifest, dict):
        return []
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    namespace = str(mcp.get("namespace") or "")
    critical = mcp.get("critical") if isinstance(mcp.get("critical"), list) else []
    if not namespace:
        return []
    return [
        {"package": name, "name": f"{namespace}_{item}"}
        for item in critical
        if isinstance(item, str)
    ]
