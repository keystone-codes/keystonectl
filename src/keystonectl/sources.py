"""Catalog sources, package resolution, and ``catalog add|list``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from packaging.version import Version

from keystonectl.catalog import (
    DEFAULT_SOURCES,
    CatalogError,
    Source,
    is_compatible,
    load_catalog,
    load_sources,
    parse_package_ref,
    sources_errors,
    url_errors,
)
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.fetch import FetchError, fetch_tree, list_tags, replace_tree
from keystonectl.output import Result, missing_summary
from keystonectl.runner import CommandRunner

_VTAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass
class ResolvedPackage:
    source_name: str
    name: str
    version: str
    ref: str
    git_url: str
    path: str
    kind: str
    keystone_mcp: str | None
    provides: dict[str, Any] | None
    commit: str | None = None
    manifest_sha256: str | None = None


@dataclass
class Resolution:
    package: ResolvedPackage | None = None
    error_id: str | None = None
    message: str = ""
    hint: str = ""
    missing: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


def cache_dir(root: Path) -> Path:
    return Path(root) / ".keystonectl"


def catalog_cache(root: Path, source_name: str) -> Path:
    return cache_dir(root) / "catalogs" / source_name


def package_cache(root: Path, source_name: str, name: str, version: str) -> Path:
    return cache_dir(root) / "packages" / source_name / name / version


def warning(warning_id: str, message: str | None = None) -> dict[str, str]:
    spec = WARNINGS[warning_id]
    return {"id": warning_id, "message": message or spec.title}


def installed_service(lock: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in lock.get("services") or []:
        if isinstance(item, dict) and (item.get("name") == name or item.get("package") == name):
            return item
    return None


def _entry(catalog: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    packages = catalog.get("packages")
    if not isinstance(packages, dict):
        return None
    entry = packages.get(name)
    return entry if isinstance(entry, dict) else None


def _versions(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    versions = entry.get("versions")
    if not isinstance(versions, list):
        return []
    return [item for item in versions if isinstance(item, dict)]


def _resolved(
    source: Source,
    name: str,
    entry: Mapping[str, Any],
    chosen: Mapping[str, Any],
) -> ResolvedPackage:
    provides = entry.get("provides") if isinstance(entry.get("provides"), dict) else None
    return ResolvedPackage(
        source_name=source.name,
        name=name,
        version=str(chosen["version"]),
        ref=str(chosen["ref"]),
        git_url=str(entry["source"]),
        path=str(entry.get("path") or "."),
        kind=str(entry.get("kind") or "service"),
        keystone_mcp=chosen.get("keystone_mcp")
        if isinstance(chosen.get("keystone_mcp"), str)
        else None,
        provides=provides,
    )


def _needs_version(entry: Mapping[str, Any], chosen: Mapping[str, Any]) -> bool:
    if entry.get("kind", "service") == "resource":
        return False
    return isinstance(chosen.get("keystone_mcp"), str)


def _incompatible(name: str, versions: list[dict[str, Any]], running: str | None) -> Resolution:
    listed = [
        {"version": item.get("version"), "keystone_mcp": item.get("keystone_mcp")}
        for item in versions
    ]
    running_text = running or "unknown"
    return Resolution(
        error_id="incompatible_keystone_mcp",
        message=(f"{name} has no version compatible with keystone-mcp {running_text}."),
        hint=ERRORS["incompatible_keystone_mcp"].action,
        result={"versions": listed, "keystone_mcp_version": running},
    )


def resolve_package(
    text: str,
    *,
    sources: list[Source],
    catalogs: Mapping[str, Mapping[str, Any]],
    lock: Mapping[str, Any],
    keystone_version: str | None,
) -> Resolution:
    """Choose a package. Source priority never breaks a name collision."""
    try:
        ref = parse_package_ref(text)
    except ValueError as exc:
        return Resolution(
            error_id="source_invalid",
            message=str(exc),
            hint=ERRORS["source_invalid"].action,
        )

    by_name = {source.name: source for source in sources}
    if ref.source is not None and ref.source not in by_name:
        return Resolution(
            error_id="package_not_found",
            message=f"Source {ref.source} is not configured.",
            hint=ERRORS["package_not_found"].action,
        )

    found: list[tuple[Source, dict[str, Any]]] = []
    for source in sources:
        if ref.source is not None and source.name != ref.source:
            continue
        catalog = catalogs.get(source.name)
        if catalog is None:
            continue
        entry = _entry(catalog, ref.name)
        if entry is not None:
            found.append((source, entry))

    if not found:
        return Resolution(
            error_id="package_not_found",
            message=f"Package {ref.name} was not found.",
            hint=ERRORS["package_not_found"].action,
        )
    if ref.source is None and len(found) > 1:
        choices = sorted(f"{source.name}/{ref.name}" for source, _entry_doc in found)
        return Resolution(
            error_id="package_ambiguous",
            message=f"Package {ref.name} is in more than one catalog.",
            hint=ERRORS["package_ambiguous"].action,
            missing=[
                {
                    "var": "package",
                    "question": "Which package should be used?",
                    "kind": "argument",
                    "choices": choices,
                    "default": None,
                    "secret": False,
                }
            ],
        )

    source, entry = found[0]
    versions = _versions(entry)
    installed = installed_service(lock, ref.name)
    chosen: dict[str, Any] | None = None
    if ref.version is None and installed and installed.get("version"):
        wanted = str(installed["version"])
        chosen = next((item for item in versions if item.get("version") == wanted), None)
        if chosen is None:
            return Resolution(
                package=ResolvedPackage(
                    source_name=str(installed.get("source") or source.name),
                    name=ref.name,
                    version=wanted,
                    ref=str(installed.get("ref") or wanted),
                    git_url=str(entry.get("source") or ""),
                    path=str(entry.get("path") or "."),
                    kind=str(entry.get("kind") or installed.get("role") or "service"),
                    keystone_mcp=installed.get("compat_keystone_mcp")
                    if isinstance(installed.get("compat_keystone_mcp"), str)
                    else None,
                    provides=entry.get("provides")
                    if isinstance(entry.get("provides"), dict)
                    else None,
                    commit=installed.get("commit")
                    if isinstance(installed.get("commit"), str)
                    else None,
                )
            )
    elif ref.version is not None:
        chosen = next((item for item in versions if item.get("version") == ref.version), None)
        if chosen is None:
            return Resolution(
                error_id="version_not_found",
                message=f"Version {ref.version} of {ref.name} is not in the catalog.",
                hint=ERRORS["version_not_found"].action,
            )
    else:
        compatible = []
        for item in versions:
            spec = item.get("keystone_mcp")
            if not isinstance(spec, str) or (
                keystone_version and is_compatible(spec, keystone_version)
            ):
                if isinstance(spec, str) and not keystone_version:
                    continue
                compatible.append(item)
        if not compatible:
            if (
                any(isinstance(item.get("keystone_mcp"), str) for item in versions)
                and not keystone_version
            ):
                from keystonectl.compat import VERSION_QUESTION

                return Resolution(
                    error_id="keystone_mcp_version_unknown",
                    message=ERRORS["keystone_mcp_version_unknown"].title,
                    hint=ERRORS["keystone_mcp_version_unknown"].action,
                    missing=[VERSION_QUESTION.as_missing()],
                )
            return _incompatible(ref.name, versions, keystone_version)
        chosen = max(compatible, key=lambda item: Version(str(item["version"])))

    assert chosen is not None
    spec = chosen.get("keystone_mcp")
    if isinstance(spec, str):
        if not keystone_version:
            from keystonectl.compat import VERSION_QUESTION

            return Resolution(
                error_id="keystone_mcp_version_unknown",
                message=ERRORS["keystone_mcp_version_unknown"].title,
                hint=ERRORS["keystone_mcp_version_unknown"].action,
                missing=[VERSION_QUESTION.as_missing()],
            )
        if not is_compatible(spec, keystone_version):
            return Resolution(
                error_id="incompatible_keystone_mcp",
                message=(
                    f"{ref.name} {chosen.get('version')} requires keystone-mcp {spec}, "
                    f"this deployment runs {keystone_version}."
                ),
                hint=ERRORS["incompatible_keystone_mcp"].action,
                result={
                    "service": ref.name,
                    "version": chosen.get("version"),
                    "required": spec,
                    "keystone_mcp_version": keystone_version,
                },
            )
    return Resolution(package=_resolved(source, ref.name, entry, chosen))


def refresh_source(
    root: Path, source: Source, runner: CommandRunner
) -> tuple[Path | None, dict[str, str] | None, str | None]:
    """Update one catalog cache. Return ``(path, warning, error_id)``."""
    dest = catalog_cache(root, source.name)
    cached = dest / "catalog.yaml"
    try:
        tree, _commit = fetch_tree(runner, source.url, source.ref)
    except FetchError as exc:
        if cached.is_file() and exc.error_id == "source_unreachable":
            return dest, warning("catalog_cached"), None
        if cached.is_file() and exc.error_id == "tag_not_found":
            return None, None, exc.error_id
        return None, None, exc.error_id
    catalog_path = tree / "catalog.yaml"
    if not catalog_path.is_file():
        shutil_rmtree(tree)
        return None, None, "catalog_invalid"
    try:
        load_catalog(catalog_path)
    except CatalogError:
        shutil_rmtree(tree)
        return None, None, "catalog_invalid"
    replace_tree(tree, dest)
    return dest, None, None


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def load_cached_catalogs(root: Path, sources: list[Source]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for source in sources:
        path = catalog_cache(root, source.name) / "catalog.yaml"
        if path.is_file():
            found[source.name] = load_catalog(path)
    return found


def refresh_all(
    root: Path, sources: list[Source], runner: CommandRunner
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], str | None]:
    warnings: list[dict[str, str]] = []
    for source in sources:
        _path, noted, error_id = refresh_source(root, source, runner)
        if error_id:
            return {}, warnings, error_id
        if noted:
            warnings.append(noted)
    try:
        catalogs = load_cached_catalogs(root, sources)
    except CatalogError:
        return {}, warnings, "catalog_invalid"
    return catalogs, warnings, None


def newest_version_tag(ls_remote_output: str) -> str | None:
    found: list[tuple[Version, str]] = []
    for line in ls_remote_output.splitlines():
        if "\t" not in line:
            continue
        _sha, ref = line.split("\t", 1)
        ref = ref.removesuffix("^{}")
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if _VTAG.fullmatch(tag):
            found.append((Version(tag[1:]), tag))
    if not found:
        return None
    found.sort()
    return found[-1][1]


def _default_priority(existing: list[Source]) -> int:
    if not existing:
        return 90
    taken = {source.priority for source in existing}
    lowest = min(taken)
    candidate = max(0, lowest - 10)
    while candidate in taken and candidate > 0:
        candidate -= 1
    if candidate not in taken:
        return candidate
    candidate = min(1000, lowest + 10)
    while candidate in taken and candidate < 1000:
        candidate += 1
    return candidate


def _dump_sources(sources: list[Source]) -> str:
    document = {
        "schema_version": 1,
        "sources": [
            {"name": source.name, "url": source.url, "ref": source.ref, "priority": source.priority}
            for source in sorted(sources, key=lambda item: item.priority, reverse=True)
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _source_invalid(url: str, problems: list[str], warnings: list[dict[str, str]]) -> Result:
    spec = ERRORS["source_invalid"]
    message = "; ".join(problems) if problems else spec.title
    return Result(
        code=spec.code,
        command="catalog add",
        summary=spec.title,
        target=url,
        warnings=warnings,
        error={"id": spec.id, "message": message, "hint": spec.action},
        result={"problems": problems},
    )


def _english_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _name_conflicts(
    root: Path,
    existing: list[Source],
    new_name: str,
    document: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Warn when the new catalog reuses a package or bundle name."""

    try:
        cached = load_cached_catalogs(root, existing)
    except CatalogError:
        cached = {}
    package_owners: dict[str, list[str]] = {}
    bundle_owners: dict[str, list[str]] = {}
    for source in existing:
        catalog = cached.get(source.name)
        if not isinstance(catalog, dict):
            continue
        packages = catalog.get("packages") if isinstance(catalog.get("packages"), dict) else {}
        bundles = catalog.get("bundles") if isinstance(catalog.get("bundles"), dict) else {}
        for name in packages:
            package_owners.setdefault(str(name), []).append(source.name)
        for name in bundles:
            bundle_owners.setdefault(str(name), []).append(source.name)
    warnings: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    new_packages = document.get("packages") if isinstance(document.get("packages"), dict) else {}
    new_bundles = document.get("bundles") if isinstance(document.get("bundles"), dict) else {}
    for kind, owners, names in (
        ("package", package_owners, new_packages),
        ("bundle", bundle_owners, new_bundles),
    ):
        warning_id = "package_name_shadowed" if kind == "package" else "bundle_name_shadowed"
        for name in names:
            previous = owners.get(str(name), [])
            if not previous:
                continue
            sources = [*previous, new_name]
            full = " or ".join(f"{source}/{name}" for source in sources)
            message = f"{name} now exists in {_english_list(sources)}; install it as {full}."
            warnings.append(warning(warning_id, message))
            conflicts.append({"kind": kind, "name": str(name), "sources": sources})
    return warnings, conflicts


def add_source(
    root: Path,
    url: str,
    runner: CommandRunner,
    *,
    name: str | None = None,
    ref: str | None = None,
    priority: int | None = None,
) -> Result:
    command = "catalog add"
    messages = url_errors(url)
    if messages:
        spec = ERRORS["source_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=url,
            error={"id": spec.id, "message": messages[0], "hint": spec.action},
            retryable=spec.retryable,
        )
    warnings: list[dict[str, str]] = []
    if url.startswith("file://"):
        warnings.append(warning("local_source"))
    chosen_ref = ref
    if chosen_ref is None:
        try:
            tag = newest_version_tag(list_tags(runner, url))
        except FetchError as exc:
            spec = ERRORS[exc.error_id]
            return Result(
                code=spec.code,
                command=command,
                summary=spec.title,
                target=url,
                error={"id": spec.id, "message": exc.message, "hint": spec.action},
                retryable=spec.retryable,
                warnings=warnings,
            )
        if tag is None:
            spec = ERRORS["tag_not_found"]
            return Result(
                code=spec.code,
                command=command,
                summary=spec.title,
                target=url,
                error={"id": spec.id, "message": "No vX.Y.Z tag was found.", "hint": spec.action},
                warnings=warnings,
            )
        chosen_ref = tag
        warnings.append(
            warning("catalog_ref_chosen", f"No --ref given; using the newest tag {tag}.")
        )
    try:
        tree, _commit = fetch_tree(runner, url, chosen_ref)
    except FetchError as exc:
        spec = ERRORS[exc.error_id]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=url,
            error={"id": spec.id, "message": exc.message, "hint": spec.action},
            retryable=spec.retryable,
            warnings=warnings,
        )
    catalog_path = tree / "catalog.yaml"
    try:
        document = load_catalog(catalog_path) if catalog_path.is_file() else None
    except CatalogError as exc:
        shutil_rmtree(tree)
        return _source_invalid(url, exc.messages, warnings)
    if document is None:
        shutil_rmtree(tree)
        return _source_invalid(url, ["catalog.yaml is missing."], warnings)
    chosen_name = name or str(document.get("name"))
    sources_path = Path(root) / "sources.yaml"
    if sources_path.is_file():
        try:
            existing = load_sources(sources_path)
        except CatalogError as exc:
            shutil_rmtree(tree)
            spec = ERRORS["source_invalid"]
            return Result(
                code=spec.code,
                command=command,
                summary=spec.title,
                target=url,
                error={"id": spec.id, "message": "; ".join(exc.messages), "hint": spec.action},
                warnings=warnings,
            )
    else:
        existing = list(DEFAULT_SOURCES)
    name_taken = any(source.name == chosen_name for source in existing)
    url_taken = any(source.url == url for source in existing)
    if name_taken or url_taken:
        shutil_rmtree(tree)
        spec = ERRORS["source_exists"]
        hint = "use --name to pick another one" if name_taken else spec.action
        message = spec.title
        if name_taken:
            message = f"A source named {chosen_name} is already configured. {hint}."
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=url,
            error={"id": spec.id, "message": message, "hint": hint},
            warnings=warnings,
        )
    chosen_priority = priority if priority is not None else _default_priority(existing)
    if any(source.priority == chosen_priority for source in existing):
        shutil_rmtree(tree)
        spec = ERRORS["priority_taken"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=url,
            error={
                "id": spec.id,
                "message": f"Priority {chosen_priority} is already used.",
                "hint": spec.action,
            },
            warnings=warnings,
        )
    updated = existing + [Source(chosen_name, url, chosen_ref, chosen_priority)]
    payload = {
        "schema_version": 1,
        "sources": [
            {"name": item.name, "url": item.url, "ref": item.ref, "priority": item.priority}
            for item in updated
        ],
    }
    problems = sources_errors(payload)
    if problems:
        shutil_rmtree(tree)
        spec = ERRORS["source_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=url,
            error={"id": spec.id, "message": "; ".join(problems), "hint": spec.action},
            warnings=warnings,
        )
    shadowed, conflicts = _name_conflicts(root, existing, chosen_name, document)
    warnings.extend(shadowed)
    if sources_path.is_file():
        backup = Path(str(sources_path) + ".bak")
        backup.write_bytes(sources_path.read_bytes())
        backup.chmod(0o600)
    sources_path.write_text(_dump_sources(updated), encoding="utf-8")
    sources_path.chmod(0o600)
    replace_tree(tree, catalog_cache(root, chosen_name))
    packages = document.get("packages") if isinstance(document.get("packages"), dict) else {}
    bundles = document.get("bundles") if isinstance(document.get("bundles"), dict) else {}
    return Result(
        code=0,
        command=command,
        summary=f"Added source {chosen_name}.",
        target=url,
        warnings=warnings,
        actions=[
            {
                "id": "catalog-add",
                "title": f"Add source {chosen_name} at {chosen_ref}",
                "status": "done",
            }
        ],
        changes=1,
        result={
            "name": chosen_name,
            "url": url,
            "ref": chosen_ref,
            "priority": chosen_priority,
            "packages": len(packages),
            "bundles": list(bundles),
            "conflicts": conflicts,
        },
    )


def list_catalogs(
    root: Path,
    runner: CommandRunner,
    *,
    keystone_version: str | None,
    lock: Mapping[str, Any],
) -> Result:
    command = "catalog list"
    try:
        sources = load_sources(Path(root) / "sources.yaml")
    except CatalogError as exc:
        spec = ERRORS["source_invalid"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            error={"id": spec.id, "message": "; ".join(exc.messages), "hint": spec.action},
        )
    catalogs, warnings, error_id = refresh_all(root, sources, runner)
    if error_id:
        spec = ERRORS[error_id]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            error={"id": spec.id, "message": spec.title, "hint": spec.action},
            retryable=spec.retryable,
            warnings=warnings,
        )
    packages: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    for source in sources:
        catalog = catalogs.get(source.name)
        if not catalog:
            continue
        for name, entry in (catalog.get("packages") or {}).items():
            if not isinstance(entry, dict):
                continue
            installed = installed_service(lock, name)
            versions = []
            for item in _versions(entry):
                spec = item.get("keystone_mcp")
                compatible = True
                if isinstance(spec, str):
                    compatible = bool(keystone_version) and is_compatible(spec, keystone_version)
                versions.append(
                    {
                        "version": item.get("version"),
                        "ref": item.get("ref"),
                        "keystone_mcp": spec,
                        "compatible": compatible,
                    }
                )
            packages.append(
                {
                    "source": source.name,
                    "name": name,
                    "kind": entry.get("kind", "service"),
                    "installed": installed.get("version") if installed else None,
                    "versions": versions,
                }
            )
        for bundle_name, bundle in (catalog.get("bundles") or {}).items():
            if isinstance(bundle, dict):
                bundles.append(
                    {
                        "source": source.name,
                        "name": bundle_name,
                        "packages": list(bundle.get("packages") or []),
                    }
                )
    return Result(
        code=0,
        command=command,
        summary=f"{len(packages)} packages in {len(sources)} sources.",
        warnings=warnings,
        result={
            "keystone_mcp_version": keystone_version,
            "sources": [
                {
                    "name": source.name,
                    "url": source.url,
                    "ref": source.ref,
                    "priority": source.priority,
                }
                for source in sources
            ],
            "packages": packages,
            "bundles": bundles,
        },
    )


def ensure_cache(root: Path) -> None:
    base = cache_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    for name in ("catalogs", "packages", "secrets", "tmp"):
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)


def failure_result(resolution: Resolution, *, command: str, target: str | None) -> Result:
    assert resolution.error_id is not None
    spec = ERRORS[resolution.error_id]
    summary = missing_summary(len(resolution.missing)) if resolution.missing else resolution.message
    return Result(
        code=spec.code,
        command=command,
        summary=summary or spec.title,
        target=target,
        missing=resolution.missing,
        warnings=resolution.warnings,
        error=None
        if resolution.missing and spec.code == 2
        else {
            "id": spec.id,
            "message": resolution.message or spec.title,
            "hint": resolution.hint or spec.action,
        },
        retryable=spec.retryable,
        result=resolution.result,
    )
