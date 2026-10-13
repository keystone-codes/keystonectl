"""service remove, upgrade, rollback, eject, and adopt."""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from keystonectl import __version__
from keystonectl.answers import Question, pkg_var
from keystonectl.errors import ERRORS
from keystonectl.generate.compose import fragment_digest, render_fragment
from keystonectl.generate.docker import compose_prefix, run
from keystonectl.generate.files import atomic_write, write_backup
from keystonectl.generate.packages_file import render_packages_file
from keystonectl.generate.registry import (
    bearer_env_name,
    identity_env_name,
    render_registry,
    render_snapshot,
    tool_names,
)
from keystonectl.layout import drop_managed_env_keys
from keystonectl.lock import save_lock, utc_now
from keystonectl.output import Result, SecretRegistry
from keystonectl.resources.binding import unbind_consumer
from keystonectl.runner import CommandRunner
from keystonectl.sources import package_cache

RETIRED_FILE = ".keystonectl/retired-tools.txt"
_OVERRIDE_NOTE = (
    "packages/{name}.override.yml is yours and was left in place; "
    "delete it if you no longer need it"
)


def _entry(lock: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in lock.get("services") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _load_lock(root: Path) -> dict[str, Any]:
    from keystonectl.lock import load_lock

    return load_lock(root / "services.lock.yaml")


def _layout(lock: Mapping[str, Any]) -> dict[str, Any]:
    layout = lock.get("layout")
    return layout if isinstance(layout, dict) else {}


def _answer(sets: Mapping[str, str], environ: Mapping[str, str], var: str) -> str | None:
    if sets.get(var):
        return sets[var]
    if environ.get(var):
        return str(environ[var])
    return None


def _confirm(
    *,
    command: str,
    target: str,
    var: str,
    question: str,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    accept: str = "yes",
    result: dict[str, Any] | None = None,
) -> Result | None:
    value = _answer(sets, environ, var)
    if value == accept:
        return None
    if interactive:
        print(question)
        typed = input("proceed? [y/N] ").strip().lower()
        if typed in {"y", "yes"} and accept == "yes":
            return None
        if typed == accept:
            return None
    missing = Question(var, question, "confirm").as_missing()
    return Result(
        code=2,
        command=command,
        summary="1 answer is missing",
        target=target,
        missing=[missing],
        result=result or {},
        changes=0,
    )


def _error(
    command: str, target: str, error_id: str, message: str, *, hint: str | None = None
) -> Result:
    spec = ERRORS[error_id]
    return Result(
        code=spec.code,
        command=command,
        summary=message,
        target=target,
        error={"id": error_id, "message": message, "hint": hint or spec.action},
        retryable=spec.retryable,
    )


def _manifest_at(
    root: Path, entry: Mapping[str, Any], version: str | None = None
) -> dict[str, Any] | None:
    source = entry.get("source")
    name = str(entry.get("package") or entry.get("name") or "")
    chosen = version or (str(entry.get("version")) if entry.get("version") else None)
    if not isinstance(source, str) or not chosen:
        return None
    path = package_cache(root, source, name, chosen) / "keystone-service.yaml"
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _requires_names(document: Mapping[str, Any]) -> list[str]:
    raw = document.get("requires")
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict) and isinstance(item.get("package"), str):
            found.append(str(item["package"]))
    return found


def _required_by(root: Path, lock: Mapping[str, Any], name: str) -> list[str]:
    found: list[str] = []
    for item in lock.get("services") or []:
        if not isinstance(item, dict) or item.get("name") == name:
            continue
        document = _manifest_at(root, item)
        if document is None:
            continue
        if name in _requires_names(document):
            found.append(str(item.get("name")))
    return found


def _policy_text(root: Path, lock: Mapping[str, Any]) -> str:
    layout = _layout(lock)
    policy = layout.get("policy")
    if not isinstance(policy, dict):
        return ""
    relative = policy.get("path") or policy.get("file")
    if isinstance(relative, str) and (root / relative).is_file():
        return (root / relative).read_text(encoding="utf-8")
    return ""


def _allow_patterns(text: str) -> list[str]:
    if not text.strip():
        return []
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict):
        return []
    roles = document.get("roles")
    patterns: list[str] = []
    items: list[Any]
    if isinstance(roles, dict):
        items = list(roles.values())
    elif isinstance(roles, list):
        items = roles
    else:
        return []
    for role in items:
        if not isinstance(role, dict):
            continue
        allow = role.get("allow")
        if isinstance(allow, list):
            patterns.extend(str(item) for item in allow if isinstance(item, str))
        elif isinstance(allow, str):
            patterns.append(allow)
    return patterns


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _read_retired(root: Path) -> list[str]:
    path = root / RETIRED_FILE
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_retired(root: Path, names: list[str]) -> None:
    path = root / RETIRED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, ("".join(f"{name}\n" for name in names)).encode("utf-8"), mode=0o644)


def prune_retired_names(root: Path, lock: dict[str, Any]) -> list[str]:
    """Drop retired names that no longer match a policy row."""

    patterns = _allow_patterns(_policy_text(root, lock))
    names = _read_retired(root)
    for item in lock.get("services") or []:
        if isinstance(item, dict):
            for tool in item.get("retired_tools") or []:
                if isinstance(tool, str) and tool not in names:
                    names.append(tool)
    kept = [name for name in names if _matches(name, patterns)]
    dropped = [name for name in names if name not in kept]
    if dropped:
        snapshot_path = root / "keystonectl" / "catalog-snapshot.yaml"
        if snapshot_path.is_file():
            document = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
            if isinstance(document, dict):
                reserved = [item for item in document.get("reserved") or [] if item not in dropped]
                document["reserved"] = reserved
                atomic_write(
                    snapshot_path,
                    render_snapshot(document).encode("utf-8"),
                    mode=0o644,
                )
        for item in lock.get("services") or []:
            if not isinstance(item, dict) or not isinstance(item.get("retired_tools"), list):
                continue
            item["retired_tools"] = [tool for tool in item["retired_tools"] if tool not in dropped]
        _write_retired(root, kept)
    return dropped


def _published(namespace: str | None, tools: list[Any]) -> list[str]:
    if not isinstance(namespace, str) or not namespace:
        return []
    return tool_names(namespace, [str(item) for item in tools if isinstance(item, str)])


def remove_service(
    root: Path,
    name: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock,
    nginx_root: Path | None = None,
) -> Result:
    command = "service remove"
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(command, name, "package_not_found", f"{name} is not installed.")
    remote = entry.get("managed") == "remote"
    blocked = _confirm(
        command=command,
        target=name,
        var=pkg_var(name, "CONFIRM_REMOVE"),
        question=f"Remove {name}? Containers stop. Volumes, buckets, and the override stay.",
        sets=sets,
        environ=environ,
        interactive=interactive,
    )
    if blocked is not None:
        return blocked
    requirers = _required_by(root, lock, name)
    if requirers:
        return _error(
            command,
            name,
            "required_by",
            f"{name} is required by {', '.join(requirers)}.",
            hint="Remove those services first.",
        )
    if entry.get("role") == "resource":
        for resource in lock.get("resources") or []:
            if not isinstance(resource, dict) or resource.get("provider") != name:
                continue
            consumers = [item for item in resource.get("consumers") or [] if isinstance(item, dict)]
            if consumers:
                listed = ", ".join(str(item.get("service")) for item in consumers)
                return _error(
                    command,
                    name,
                    "resource_in_use",
                    f"{name} still has consumers: {listed}.",
                )
    layout = _layout(lock)
    project = str(layout.get("project") or "")
    containers = entry.get("containers") if isinstance(entry.get("containers"), dict) else {}
    services = [str(spec.get("service")) for spec in containers.values() if isinstance(spec, dict)]
    fragment = root / "packages" / f"{name}.yml"
    if services and not remote:
        run(
            runner,
            [
                *compose_prefix(root, layout, project=project or None),
                "rm",
                "-s",
                "-f",
                *services,
            ],
            cwd=root,
        )
    managed = entry.get("managed")
    if managed == "generated" and fragment.is_file():
        fragment.unlink()
    kept: list[str] = []
    if managed == "ejected" and fragment.is_file():
        kept.append(f"packages/{name}.yml")
    override = root / "packages" / f"{name}.override.yml"
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    files = [str(item) for item in env.get("files") or [] if isinstance(item, str)]
    provider = entry.get("role") == "resource"
    for relative in files:
        path = root / relative
        if provider and path.name == f"{name}.env":
            kept.append(relative)
            continue
        if path.is_file():
            path.unlink()
    if override.is_file():
        kept.append(f"packages/{name}.override.yml")
    _rewrite_packages(root, lock, name)
    if not remote:
        run(runner, ["docker", "network", "rm", f"ks-pkg-{name}"], cwd=root)
    _drop_registry(root, entry)
    _drop_env_keys(root, entry)
    patterns = _allow_patterns(_policy_text(root, lock))
    published = _published(
        entry.get("namespace") if isinstance(entry.get("namespace"), str) else None,
        list(entry.get("tools") or []),
    )
    orphaned = [tool for tool in published if _matches(tool, patterns)]
    _move_reserved(root, lock, published, orphaned)
    retired = _read_retired(root)
    for tool in orphaned:
        if tool not in retired:
            retired.append(tool)
    _write_retired(root, retired)
    warnings = []
    if orphaned:
        warnings.append(
            {
                "id": "policy_rows_orphaned",
                "message": (
                    "delete these policy rows; the tool names stay reserved until you do: "
                    + ", ".join(orphaned)
                ),
            }
        )
    prune_retired_names(root, lock)
    kept_messages = unbind_consumer(lock, name)
    kept.extend(kept_messages)
    manifest = _manifest_at(root, entry)
    if isinstance(manifest, dict):
        containers_doc = (
            manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
        )
        for spec in containers_doc.values():
            if not isinstance(spec, dict):
                continue
            for volume in spec.get("volumes") or []:
                if isinstance(volume, dict) and isinstance(volume.get("name"), str):
                    kept.append(f"ks-pkg-{name}-{volume['name']}")
    public = entry.get("public") if isinstance(entry.get("public"), dict) else {}
    if isinstance(public.get("domain"), str):
        kept.append(f"certificate {public['domain']}")
        _remove_vhost(root, runner, name, public, nginx_root=nginx_root)
    lock["services"] = [
        item
        for item in lock.get("services") or []
        if not (isinstance(item, dict) and item.get("name") == name)
    ]
    if provider:
        lock["resources"] = [
            item
            for item in lock.get("resources") or []
            if not (isinstance(item, dict) and item.get("provider") == name)
        ]
    lock["updated_by"] = f"keystonectl {__version__}"
    save_lock(root / "services.lock.yaml", lock, now=clock())
    note = _OVERRIDE_NOTE.format(name=name) if override.is_file() else ""
    summary = f"Removed {name}."
    if note:
        summary = f"{summary} {note}"
    return Result(
        code=0,
        command=command,
        summary=summary,
        target=name,
        warnings=warnings,
        result={"kept": kept, "note": note} if note else {"kept": kept},
        changes=1,
    )


def _rewrite_packages(root: Path, lock: Mapping[str, Any], removed: str) -> None:
    layout = _layout(lock)
    names: list[tuple[str, bool]] = []
    for item in lock.get("services") or []:
        if (
            not isinstance(item, dict)
            or item.get("name") == removed
            or item.get("managed") == "remote"
        ):
            continue
        package = str(item["name"])
        names.append((package, (root / "packages" / f"{package}.override.yml").is_file()))
    network = (
        layout.get("upstreams_network") if isinstance(layout.get("upstreams_network"), dict) else {}
    )
    text = render_packages_file(
        keystone_service=str(layout.get("keystone_service") or "keystone"),
        packages=names,
        attach_network=network.get("attached_by") != "operator",
        include_default_network=bool(network.get("include_default_network")),
    )
    (root / "docker-compose.packages.yml").write_text(text, encoding="utf-8")


def _drop_registry(root: Path, entry: Mapping[str, Any]) -> None:
    path = root / "keystonectl" / "upstreams.yaml"
    if not path.is_file():
        return
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = document.get("entries") if isinstance(document, dict) else []
    registry_name = entry.get("registry_name")
    kept = [
        item
        for item in entries or []
        if isinstance(item, dict) and item.get("name") != registry_name
    ]
    if isinstance(document, dict):
        write_backup(path)
        atomic_write(path, render_registry(kept).encode("utf-8"), mode=0o644)
    snapshot_path = root / "keystonectl" / "catalog-snapshot.yaml"
    snapshot = {}
    if snapshot_path.is_file():
        loaded = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
        snapshot = loaded if isinstance(loaded, dict) else {}
    services = [
        item
        for item in []  # the removed service is excluded by the caller after this returns
    ]
    del services
    namespace = entry.get("namespace")
    tools = entry.get("tools") if isinstance(entry.get("tools"), list) else []
    owned = set(_published(namespace if isinstance(namespace, str) else None, tools))
    active = [item for item in snapshot.get("active") or [] if item not in owned]
    snapshot["active"] = active
    write_backup(snapshot_path)
    atomic_write(snapshot_path, render_snapshot(snapshot).encode("utf-8"), mode=0o644)


def _move_reserved(
    root: Path, lock: Mapping[str, Any], published: list[str], orphaned: list[str]
) -> None:
    path = root / "keystonectl" / "catalog-snapshot.yaml"
    document: dict[str, Any] = {"active": [], "reserved": [], "wip": []}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            document = loaded
    active = [item for item in document.get("active") or [] if item not in published]
    reserved = list(document.get("reserved") or [])
    for name in orphaned:
        if name not in reserved:
            reserved.append(name)
    document["active"] = active
    document["reserved"] = reserved
    atomic_write(path, render_snapshot(document).encode("utf-8"), mode=0o644)
    del lock


def _drop_env_keys(root: Path, entry: Mapping[str, Any]) -> None:
    namespace = entry.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        return
    path = root / ".env"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    updated = drop_managed_env_keys(
        text,
        {bearer_env_name(namespace), identity_env_name(namespace)},
    )
    atomic_write(path, updated.encode("utf-8"), mode=0o600)


def _remove_vhost(
    root: Path,
    runner: CommandRunner,
    name: str,
    public: Mapping[str, Any],
    *,
    nginx_root: Path | None = None,
) -> None:
    from keystonectl.public import _nginx_dirs, site_name
    from keystonectl.tls import Tls

    available, enabled, _conf = _nginx_dirs(_Host(root, nginx_root))
    site = site_name(name)
    tls = Tls(runner, clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC))
    link = enabled / site
    target = link.resolve() if link.is_symlink() else None
    if link.exists() or link.is_symlink():
        tls._run(["rm", "-f", str(link)])
    site_file = available / site
    if site_file.is_file():
        tls._run(["rm", "-f", str(site_file)])
    took = public.get("took_over")
    remembered = None
    if isinstance(took, str) and took:
        restored = enabled / took
        destination = available / took
        if destination.is_file() and not restored.exists():
            tls._run(["ln", "-s", str(destination), str(restored)])
            remembered = target
    if remembered is not None:
        del remembered
    ok, _detail = __import__("keystonectl.public", fromlist=["_nginx_test"])._nginx_test(tls)
    if ok:
        from keystonectl.public import _reload

        _reload(tls)


class _Host:
    def __init__(self, root: Path, nginx_root: Path | None = None) -> None:
        self.root = root
        self.nginx_root = nginx_root


def _catalog_versions(root: Path, name: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    catalogs = root / ".keystonectl" / "catalogs"
    if not catalogs.is_dir():
        return found
    for path in catalogs.glob("*/catalog.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        packages = document.get("packages") if isinstance(document, dict) else None
        if not isinstance(packages, dict) or name not in packages:
            continue
        entry = packages[name]
        if isinstance(entry, dict):
            for item in entry.get("versions") or []:
                if isinstance(item, dict) and isinstance(item.get("version"), str):
                    found.append(item)
    return found


def _newer_version(
    current: str, versions: list[dict[str, Any]], keystone: str | None
) -> str | None:
    try:
        current_version = Version(current)
    except InvalidVersion:
        return None
    best: Version | None = None
    best_text: str | None = None
    for item in versions:
        text = str(item.get("version"))
        try:
            candidate = Version(text)
        except InvalidVersion:
            continue
        if candidate <= current_version:
            continue
        spec = item.get("keystone_mcp")
        if isinstance(spec, str) and keystone:
            try:
                if Version(keystone) not in SpecifierSet(spec):
                    continue
            except InvalidSpecifier:
                continue
        if best is None or candidate > best:
            best = candidate
            best_text = text
    return best_text


def _tool_set(document: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(document, dict):
        return []
    mcp = document.get("mcp") if isinstance(document.get("mcp"), dict) else {}
    return [str(item) for item in mcp.get("tools") or [] if isinstance(item, str)]


def _roles(document: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(document, dict) or not isinstance(document.get("containers"), dict):
        return set()
    return {str(role) for role in document["containers"]}


def upgrade_service(
    root: Path,
    ref: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock,
) -> Result:
    command = "service upgrade"
    name, _sep, requested = ref.partition("@")
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(command, ref, "package_not_found", f"{name} is not installed.")
    if entry.get("managed") == "remote":
        return _error(
            command,
            ref,
            "not_package",
            "a remote service has no package; use add-remote to change its url",
        )
    if entry.get("managed") == "generated":
        path = root / "packages" / f"{name}.yml"
        expected = entry.get("fragment_sha256")
        if path.is_file() and isinstance(expected, str) and expected:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                return _error(
                    command,
                    ref,
                    "fragment_modified",
                    f"packages/{name}.yml was edited by hand (sha256 differs from services.lock.yaml)",
                    hint=(
                        f"Move your changes to packages/{name}.override.yml, "
                        f"or run `keystonectl service eject {name}`, then upgrade again."
                    ),
                )
    current = str(entry.get("version") or "")
    layout = _layout(lock)
    keystone = layout.get("keystone_mcp_version")
    keystone_text = str(keystone) if isinstance(keystone, str) else None
    target = requested or _newer_version(current, _catalog_versions(root, name), keystone_text)
    if target is None or target == current:
        return Result(
            code=0,
            command=command,
            summary=f"{name} is up to date.",
            target=name,
            result={"up_to_date": True},
            changes=0,
        )
    before = _manifest_at(root, entry, current)
    after = _manifest_at(root, entry, target)
    if after is None:
        return _error(
            command, f"{name}@{target}", "version_not_found", f"{target} is not in the cache."
        )
    removed = [item for item in _tool_set(before) if item not in _tool_set(after)]
    added = [item for item in _tool_set(after) if item not in _tool_set(before)]
    roles_before = _roles(before)
    roles_after = _roles(after)
    diff = {
        "from": current,
        "to": target,
        "tools_removed": removed,
        "tools_added": added,
        "containers_added": sorted(roles_after - roles_before),
        "containers_removed": sorted(roles_before - roles_after),
        "policy_rows_stop_matching": [],
        "tools_unreachable": [],
    }
    stop, unreachable = _policy_delta(root, lock, entry, before, after, runner)
    diff["policy_rows_stop_matching"] = stop
    diff["tools_unreachable"] = unreachable
    namespace = entry.get("namespace") if isinstance(entry.get("namespace"), str) else ""
    question = (
        f"Upgrade {name} {current} -> {target}? "
        f"Removed tools: {', '.join(removed) or 'none'}. "
        f"Added: {', '.join(added) or 'none'}. "
        f"Policy rows that stop matching: {', '.join(diff['policy_rows_stop_matching']) or 'none'}."
    )
    blocked = _confirm(
        command=command,
        target=f"{name}@{target}",
        var=pkg_var(name, "CONFIRM_UPGRADE"),
        question=question,
        sets=sets,
        environ=environ,
        interactive=interactive,
        accept=target,
        result={"diff": diff},
    )
    if blocked is not None:
        return blocked
    if entry.get("managed") == "ejected":
        return _upgrade_ejected(root, lock, entry, after, target, diff, clock)
    _write_baks(root, entry)
    entry["previous"] = {
        "version": current,
        "ref": entry.get("ref"),
        "commit": entry.get("commit"),
        "manifest_sha256": entry.get("manifest_sha256"),
        "containers": entry.get("containers"),
        "fragment_sha256": entry.get("fragment_sha256"),
        "tools": list(entry.get("tools") or []),
    }
    retired = list(entry.get("retired_tools") or [])
    patterns = _allow_patterns(_policy_text(root, lock))
    for tool in _published(namespace or None, removed):
        if _matches(tool, patterns) and tool not in retired:
            retired.append(tool)
    entry["retired_tools"] = retired
    save_lock(root / "services.lock.yaml", lock, now=clock())
    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.plan import run_service

    applied = run_service(
        root,
        "apply",
        f"{name}@{target}",
        runner=runner,
        secrets=secrets,
        sets=dict(sets),
        environ=environ,
        interactive=False,
        clock=clock,
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    applied.command = command
    applied.target = f"{name}@{target}"
    applied.result = {**applied.result, "diff": diff}
    if applied.error and applied.error.get("id") == "service_unhealthy":
        applied.error["hint"] = f"keystonectl service rollback {name}"
    return applied


def _diff_sections(text: str) -> tuple[list[str], list[str]]:
    """Parse ``catalog diff`` sections. Also accept the validate stderr phrase."""

    from keystonectl.generate.validators import unmatched_patterns

    unmatched: list[str] = []
    unreachable: list[str] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Unmatched allow patterns"):
            section = "unmatched"
            continue
        if stripped.startswith("Only in live"):
            section = "unreachable"
            continue
        if stripped in {"", "(none)"}:
            continue
        if stripped.startswith("- ") and section:
            item = stripped[2:].strip()
            if item and item != "(none)":
                if section == "unmatched":
                    unmatched.append(item)
                else:
                    unreachable.append(item)
            continue
        unmatched.extend(unmatched_patterns(line))
    return unmatched, unreachable


def _policy_delta(
    root: Path,
    lock: Mapping[str, Any],
    entry: Mapping[str, Any],
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    runner: CommandRunner,
) -> tuple[list[str], list[str]]:
    image = str(_layout(lock).get("keystone_image") or "")
    if not image:
        return [], []
    staging = root / ".keystonectl" / "upgrade-diff"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "live.txt").write_text("", encoding="utf-8")
    policy = root / "policy" / "rbac-policy.yaml"
    layout_policy = _layout(lock).get("policy")
    if isinstance(layout_policy, str) and (root / layout_policy).is_file():
        policy = root / layout_policy
    elif isinstance(layout_policy, dict):
        relative = layout_policy.get("path") or layout_policy.get("file")
        if isinstance(relative, str) and (root / relative).is_file():
            policy = root / relative
    if not policy.is_file():
        return [], []

    def snapshot_for(document: Mapping[str, Any] | None) -> None:
        tools = _tool_set(document)
        namespace = entry.get("namespace") if isinstance(entry.get("namespace"), str) else ""
        payload = {
            "active": tool_names(namespace, tools) if namespace else [],
            "reserved": [],
            "wip": [],
        }
        (staging / "catalog-snapshot.yaml").write_text(render_snapshot(payload), encoding="utf-8")

    def run_diff() -> tuple[list[str], list[str]]:
        result = run(
            runner,
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{policy}:/ksctl-policy/policy.yaml:ro",
                "-v",
                f"{staging}:/ksctl:ro",
                "--entrypoint",
                "keystone-mcp",
                image,
                "catalog",
                "diff",
                "--policy",
                "/ksctl-policy/policy.yaml",
                "--snapshot",
                "/ksctl/catalog-snapshot.yaml",
                "--live-file",
                "/ksctl/live.txt",
            ],
            cwd=root,
        )
        return _diff_sections(result.stdout + "\n" + result.stderr)

    snapshot_for(before)
    old_unmatched, old_unreachable = run_diff()
    snapshot_for(after)
    new_unmatched, new_unreachable = run_diff()
    return (
        sorted(set(new_unmatched) - set(old_unmatched)),
        sorted(set(new_unreachable) - set(old_unreachable)),
    )


def _write_baks(root: Path, entry: Mapping[str, Any]) -> None:
    name = str(entry.get("name"))
    fragment = root / "packages" / f"{name}.yml"
    if fragment.is_file():
        destination = fragment.with_name(fragment.name + ".bak")
        shutil.copy2(fragment, destination)
        os.chmod(destination, 0o600)
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    for relative in env.get("files") or []:
        if not isinstance(relative, str):
            continue
        path = root / relative
        if not path.is_file():
            continue
        destination = path.with_name(path.name + ".bak")
        shutil.copy2(path, destination)
        os.chmod(destination, 0o600)


def _upgrade_ejected(
    root: Path,
    lock: dict[str, Any],
    entry: dict[str, Any],
    manifest: Mapping[str, Any],
    target: str,
    diff: dict[str, Any],
    clock,
) -> Result:
    name = str(entry.get("name"))
    entry["version"] = target
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    entry["containers"] = {
        role: {
            "service": f"{name}-{role}",
            "image": str(spec.get("image") or ""),
            "init": bool(spec.get("init")),
        }
        for role, spec in containers.items()
        if isinstance(spec, dict)
    }
    steps = [
        f"{role}: {spec.get('image')}"
        for role, spec in containers.items()
        if isinstance(spec, dict)
    ]
    save_lock(root / "services.lock.yaml", lock, now=clock())
    return Result(
        code=0,
        command="service upgrade",
        summary=f"Recorded {name} {target}. The fragment is yours.",
        target=f"{name}@{target}",
        result={"diff": diff, "manual_steps": steps},
        changes=1,
    )


def rollback_service(
    root: Path,
    name: str,
    *,
    runner: CommandRunner,
    clock,
) -> Result:
    command = "service rollback"
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(command, name, "package_not_found", f"{name} is not installed.")
    if entry.get("managed") == "remote":
        return _error(
            command,
            name,
            "not_package",
            "a remote service has no package; use add-remote to change its url",
        )
    previous = entry.get("previous")
    fragment_bak = root / "packages" / f"{name}.yml.bak"
    if not isinstance(previous, dict) or not fragment_bak.is_file():
        return _error(command, name, "no_previous_version", ERRORS["no_previous_version"].title)
    fragment = root / "packages" / f"{name}.yml"
    fragment.write_bytes(fragment_bak.read_bytes())
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    for relative in env.get("files") or []:
        if not isinstance(relative, str):
            continue
        path = root / relative
        backup = path.with_name(path.name + ".bak")
        if backup.is_file():
            path.write_bytes(backup.read_bytes())
            os.chmod(path, 0o600)
    for field in (
        "version",
        "ref",
        "commit",
        "manifest_sha256",
        "containers",
        "fragment_sha256",
        "tools",
    ):
        if field in previous:
            entry[field] = previous[field]
    if isinstance(previous.get("fragment_sha256"), str):
        entry["fragment_sha256"] = previous["fragment_sha256"]
    else:
        entry["fragment_sha256"] = fragment_digest(fragment.read_text(encoding="utf-8"))
    entry["previous"] = None
    entry["updated_at"] = utc_now(clock())
    save_lock(root / "services.lock.yaml", lock, now=clock())
    layout = _layout(lock)
    project = str(layout.get("project") or "")
    containers = entry.get("containers") if isinstance(entry.get("containers"), dict) else {}
    services = [str(spec.get("service")) for spec in containers.values() if isinstance(spec, dict)]
    if services:
        run(
            runner,
            [
                *compose_prefix(root, layout, project=project or None),
                "up",
                "-d",
                "--force-recreate",
                *services,
            ],
            cwd=root,
        )
    return Result(
        code=0,
        command=command,
        summary=f"Restored {name} {entry.get('version')}.",
        target=name,
        result={"version": entry.get("version"), "fragment_sha256": entry.get("fragment_sha256")},
        changes=1,
    )


def eject_service(root: Path, name: str, *, clock) -> Result:
    command = "service eject"
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(command, name, "package_not_found", f"{name} is not installed.")
    if entry.get("managed") == "remote":
        return _error(command, name, "not_ejectable", ERRORS["not_ejectable"].title)
    if entry.get("managed") == "ejected":
        return Result(
            code=0,
            command=command,
            summary=f"{name} is already ejected.",
            target=name,
            result={"unchanged": True},
            changes=0,
        )
    entry["managed"] = "ejected"
    entry["fragment_sha256"] = None
    entry["updated_at"] = utc_now(clock())
    save_lock(root / "services.lock.yaml", lock, now=clock())
    return Result(
        code=0,
        command=command,
        summary=(
            f"packages/{name}.yml is yours now; keystonectl will not rewrite it. "
            f"Run `keystonectl service adopt {name}` to hand it back."
        ),
        target=name,
        changes=1,
    )


def _fragment_changes(before: str, after: str) -> list[dict[str, Any]]:
    try:
        old = yaml.safe_load(before) or {}
        new = yaml.safe_load(after) or {}
    except yaml.YAMLError:
        return [{"service": "", "keys": ["text"]}]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [{"service": "", "keys": ["text"]}]
    old_services = old.get("services") if isinstance(old.get("services"), dict) else {}
    new_services = new.get("services") if isinstance(new.get("services"), dict) else {}
    changes: list[dict[str, Any]] = []
    for service in sorted(set(old_services) | set(new_services)):
        left = old_services.get(service) if isinstance(old_services.get(service), dict) else {}
        right = new_services.get(service) if isinstance(new_services.get(service), dict) else {}
        keys = sorted({*left, *right} - {key for key in left if left.get(key) == right.get(key)})
        if keys:
            changes.append({"service": str(service), "keys": keys})
    return changes


def adopt_service(
    root: Path,
    name: str,
    *,
    runner: CommandRunner,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock,
    json_mode: bool,
) -> Result:
    command = "service adopt"
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(command, name, "package_not_found", f"{name} is not installed.")
    if entry.get("managed") != "ejected":
        return _error(command, name, "not_ejected", ERRORS["not_ejected"].title)
    document = _manifest_at(root, entry)
    if document is None:
        return _error(command, name, "manifest_invalid", f"{name} has no cached manifest.")
    public = entry.get("public") if isinstance(entry.get("public"), dict) else {}
    port = public.get("host_port") if isinstance(public.get("host_port"), int) else None
    domains = [
        str(item.get("public", {}).get("domain"))
        for item in lock.get("services") or []
        if isinstance(item, dict)
        and isinstance(item.get("public"), dict)
        and item["public"].get("state") == "ready"
        and isinstance(item["public"].get("domain"), str)
    ]
    rendered = render_fragment(document, entry, host_port=port, extra_hosts=domains)
    current_path = root / "packages" / f"{name}.yml"
    current = current_path.read_text(encoding="utf-8") if current_path.is_file() else ""
    changes = _fragment_changes(current, rendered)
    blocked = _confirm(
        command=command,
        target=name,
        var=pkg_var(name, "CONFIRM_ADOPT"),
        question=f"Replace packages/{name}.yml with a generated fragment?",
        sets=sets,
        environ=environ,
        interactive=interactive,
        result={"changes": changes},
    )
    if blocked is not None:
        if not json_mode:
            diff = "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    fromfile=f"packages/{name}.yml",
                    tofile="generated",
                )
            )
            blocked.summary = diff or blocked.summary
        return blocked
    backup = root / "packages" / f"{name}.yml.ejected.bak"
    if current_path.is_file():
        backup.write_bytes(current_path.read_bytes())
        os.chmod(backup, 0o600)
    current_path.write_text(rendered, encoding="utf-8")
    os.chmod(current_path, 0o644)
    entry["managed"] = "generated"
    entry["fragment_sha256"] = fragment_digest(rendered)
    entry["fragment"] = f"packages/{name}.yml"
    entry["updated_at"] = utc_now(clock())
    save_lock(root / "services.lock.yaml", lock, now=clock())
    layout = _layout(lock)
    project = str(layout.get("project") or "")
    containers = entry.get("containers") if isinstance(entry.get("containers"), dict) else {}
    services = [str(spec.get("service")) for spec in containers.values() if isinstance(spec, dict)]
    if services:
        run(
            runner,
            [
                *compose_prefix(root, layout, project=project or None),
                "up",
                "-d",
                "--force-recreate",
                *services,
            ],
            cwd=root,
        )
    return Result(
        code=0,
        command=command,
        summary=(
            f"packages/{name}.yml is generated again. "
            f"Your previous file is in packages/{name}.yml.ejected.bak; "
            "move what you need into the override."
        ),
        target=name,
        result={"changes": changes},
        changes=1,
    )


def register_commands(subparsers: argparse._SubParsersAction) -> None:
    for name, help_text in (
        ("remove", "Remove a service. Volumes, buckets, and the override stay."),
        ("upgrade", "Upgrade a service and show the policy diff."),
        ("rollback", "Restore the previous upgrade."),
        ("eject", "Stop generating the compose fragment."),
        ("adopt", "Generate the fragment again after eject."),
    ):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--root", default=None)
        parser.add_argument("--non-interactive", action="store_true")
        parser.add_argument("--set", action="append", default=None)
        parser.add_argument("name" if name != "upgrade" else "ref")
