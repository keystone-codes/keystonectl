"""Catalog and source documents (catalog.yaml, sources.yaml)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

CATALOG_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "catalog-v1.json"
SOURCES_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "sources-v1.json"

_NAME = r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?"
_PKG = r"(?!.*--)[a-z][a-z0-9-]{1,38}[a-z0-9]"
_VER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_REF = re.compile(rf"^(?:(?P<source>{_NAME})/)?(?P<name>{_PKG})(?:@(?P<version>{_VER}))?$")
_TAG_OR_SHA = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}|[0-9a-f]{40})$")
_BRANCHES = frozenset({"main", "master", "develop", "trunk", "HEAD"})
_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,128}$")
_URLS = (
    re.compile(r"^https://\S+$"),
    re.compile(r"^ssh://\S+$"),
    re.compile(r"^git@[A-Za-z0-9.-]+:\S+$"),
    re.compile(r"^file:///\S+$"),
)

DEFAULT_CATALOG_REF = "v1.3.0"

_CATALOG_SCHEMA = json.loads(CATALOG_SCHEMA_PATH.read_text(encoding="utf-8"))
_SOURCES_SCHEMA = json.loads(SOURCES_SCHEMA_PATH.read_text(encoding="utf-8"))
_CATALOG_VALIDATOR = Draft202012Validator(_CATALOG_SCHEMA)
_SOURCES_VALIDATOR = Draft202012Validator(_SOURCES_SCHEMA)


class CatalogError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    ref: str
    priority: int


@dataclass(frozen=True)
class PackageRef:
    source: str | None
    name: str
    version: str | None


DEFAULT_SOURCES: tuple[Source, ...] = (
    Source(
        "keystone-codes",
        "https://github.com/keystone-codes/keystone-catalog.git",
        DEFAULT_CATALOG_REF,
        100,
    ),
)


def parse_package_ref(text: str) -> PackageRef:
    match = _REF.match(text)
    if not match:
        raise ValueError(f"package reference must look like [source/]name[@X.Y.Z]: '{text}'")
    return PackageRef(match.group("source"), match.group("name"), match.group("version"))


def keystone_mcp_range_errors(spec: str) -> list[str]:
    text = spec.strip()
    try:
        parsed = SpecifierSet(text)
    except (InvalidVersion, ValueError):
        return [f"keystone_mcp range '{spec}' is not a valid version specifier"]
    for item in parsed:
        if item.operator not in {">=", "<", "!="}:
            return [
                (
                    f"keystone_mcp range '{spec}': operator '{item.operator}' is not allowed; "
                    "use >=, < and !="
                )
            ]
    operators = [item.operator for item in parsed]
    lower = operators.count(">=")
    upper = operators.count("<")
    if lower != 1 or upper != 1:
        return [
            (
                f"keystone_mcp range '{spec}' must have exactly one lower bound (>=) "
                "and one upper bound (<)"
            )
        ]
    low = next(item for item in parsed if item.operator == ">=")
    high = next(item for item in parsed if item.operator == "<")
    try:
        if Version(low.version) >= Version(high.version):
            return [
                (
                    f"keystone_mcp range '{spec}' is empty: "
                    "the lower bound is not below the upper bound"
                )
            ]
    except InvalidVersion:
        return [f"keystone_mcp range '{spec}' is not a valid version specifier"]
    return []


def is_compatible(spec: str, version: str) -> bool:
    if keystone_mcp_range_errors(spec):
        return False
    try:
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except InvalidVersion:
        return False


def url_errors(url: object) -> list[str]:
    if not isinstance(url, str):
        return ["source url must be a string"]
    if url.startswith("http://"):
        return ["http:// URLs are not allowed"]
    if any(pattern.match(url) for pattern in _URLS):
        return []
    return [f"invalid source url: {url}"]


def catalog_errors(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["$: must be an object"]
    errors = _validator_messages(document, _CATALOG_VALIDATOR)
    errors.extend(_catalog_semantic(document))
    return _dedupe(errors)


def sources_errors(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["$: must be an object"]
    errors = _validator_messages(document, _SOURCES_VALIDATOR)
    errors.extend(_sources_semantic(document))
    return _dedupe(errors)


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CatalogError([f"{path}: file not found"])
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError([f"{path}: {exc}"]) from exc
    messages = catalog_errors(document)
    if messages:
        raise CatalogError(messages)
    assert isinstance(document, dict)
    return document


def load_sources(path: Path) -> list[Source]:
    if not path.is_file():
        return sorted(DEFAULT_SOURCES, key=lambda source: source.priority, reverse=True)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError([f"{path}: {exc}"]) from exc
    messages = sources_errors(document)
    if messages:
        raise CatalogError(messages)
    assert isinstance(document, dict)
    sources = [
        Source(
            name=item["name"],
            url=item["url"],
            ref=item["ref"],
            priority=item["priority"],
        )
        for item in document["sources"]
    ]
    return sorted(sources, key=lambda source: source.priority, reverse=True)


def _catalog_semantic(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    packages = document.get("packages")
    if not isinstance(packages, dict):
        return errors
    for pkg_name, pkg in packages.items():
        if not isinstance(pkg, dict):
            continue
        base = f"packages.{pkg_name}"
        kind = pkg.get("kind", "service")
        errors.extend(f"{base}.source: {item}" for item in url_errors(pkg.get("source")))
        path = pkg.get("path", ".")
        if path != "." and (not isinstance(path, str) or not _PATH.match(path)):
            errors.append(f"{base}.path: invalid path")
        provides = pkg.get("provides")
        if kind == "resource":
            if not isinstance(provides, dict):
                errors.append(f"{base}.provides: required for a resource")
            else:
                if provides.get("kind") not in {"s3", "postgres"}:
                    errors.append(f"{base}.provides.kind: must be s3 or postgres")
                if provides.get("version") != 1:
                    errors.append(f"{base}.provides.version: must be 1")
        elif provides is not None:
            errors.append(f"{base}.provides is not allowed for kind: service")
        versions = pkg.get("versions")
        if not isinstance(versions, list):
            continue
        seen_versions: set[str] = set()
        for index, item in enumerate(versions):
            if not isinstance(item, dict):
                continue
            vpath = f"{base}.versions[{index}]"
            version = item.get("version")
            if isinstance(version, str) and version in seen_versions:
                errors.append(f"{base}.versions: duplicate version {version}")
            elif isinstance(version, str):
                seen_versions.add(version)
            errors.extend(_ref_errors(item.get("ref"), f"{vpath}.ref"))
            compat = item.get("keystone_mcp")
            if kind == "service":
                if isinstance(compat, str):
                    errors.extend(
                        f"{vpath}.keystone_mcp: {msg}" for msg in keystone_mcp_range_errors(compat)
                    )
            elif compat is not None:
                errors.append(f"{vpath}.keystone_mcp is not allowed for kind: resource")
    bundles = document.get("bundles", {})
    if not isinstance(bundles, dict):
        return errors
    for bundle_name, bundle in bundles.items():
        if not isinstance(bundle, dict):
            continue
        bpath = f"bundles.{bundle_name}"
        names = bundle.get("packages")
        if not isinstance(names, list):
            continue
        for index, pkg_name in enumerate(names):
            if pkg_name not in packages:
                errors.append(f"{bpath}.packages[{index}]: unknown package '{pkg_name}'")
                continue
            entry = packages.get(pkg_name)
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind", "service")
            if kind == "resource":
                errors.append(
                    f"{bpath}.packages[{index}]: '{pkg_name}' is a resource; "
                    "resources are installed through needs"
                )
    return errors


def _sources_semantic(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sources = document.get("sources")
    if not isinstance(sources, list):
        return errors
    names: set[str] = set()
    priorities: set[int] = set()
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            continue
        path = f"sources[{index}]"
        name = item.get("name")
        if isinstance(name, str) and name in names:
            errors.append(f"sources: two sources share name {name}")
        elif isinstance(name, str):
            names.add(name)
        errors.extend(f"{path}.url: {msg}" for msg in url_errors(item.get("url")))
        errors.extend(_ref_errors(item.get("ref"), f"{path}.ref"))
        priority = item.get("priority")
        if isinstance(priority, int) and not isinstance(priority, bool):
            if priority in priorities:
                errors.append(f"sources: two sources share priority {priority}")
            else:
                priorities.add(priority)
    return errors


def _ref_errors(ref: object, path: str) -> list[str]:
    if not isinstance(ref, str) or not _TAG_OR_SHA.match(ref):
        return [f"{path}: ref must be a tag or a 40-character commit"]
    if ref in _BRANCHES:
        return [f"{path}: '{ref}' looks like a branch; use a tag or a commit"]
    return []


def _validator_messages(document: object, validator: Draft202012Validator) -> list[str]:
    messages: list[str] = []
    for error in validator.iter_errors(document):
        path = _format_path(tuple(error.absolute_path))
        if error.validator == "additionalProperties":
            schema = error.schema if isinstance(error.schema, dict) else {}
            instance = error.instance if isinstance(error.instance, dict) else {}
            properties = schema.get("properties", {})
            patterns = [re.compile(pattern) for pattern in schema.get("patternProperties", {})]
            extras = [
                key
                for key in instance
                if isinstance(key, str)
                and key not in properties
                and not any(pattern.search(key) for pattern in patterns)
            ]
            if extras:
                messages.append(
                    f"{path}: additional properties are not allowed: {', '.join(extras)}"
                )
            continue
        messages.append(f"{path}: {error.message}")
    return messages


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


def _dedupe(messages: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for message in messages:
        if message in seen:
            continue
        seen.add(message)
        ordered.append(message)
    return ordered
