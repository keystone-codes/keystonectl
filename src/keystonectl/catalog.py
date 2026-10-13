"""Catalog and source documents.

The catalog card replaces this module. The JSON Schemas catalog-v1.json and
sources-v1.json are not shipped here; structural checks live in this module
so package resolution can run before that card lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

_NAME = r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?"
_PKG = r"[a-z][a-z0-9-]{1,38}[a-z0-9]"
_VER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_REF = re.compile(rf"^(?:(?P<source>{_NAME})/)?(?P<name>{_PKG})(?:@(?P<version>{_VER}))?$")
_URLS = (
    re.compile(r"^https://\S+$"),
    re.compile(r"^ssh://\S+$"),
    re.compile(r"^git@[A-Za-z0-9.-]+:\S+$"),
    re.compile(r"^file:///\S+$"),
)
_TAG_OR_SHA = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}|[0-9a-f]{40})$")
_BRANCHES = frozenset({"main", "master", "develop", "trunk", "HEAD"})
_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,128}$")

DEFAULT_CATALOG_REF = "v1.0.0"


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
    operators = [item.operator for item in parsed]
    for item in parsed:
        if item.operator not in {">=", "<", "!="}:
            return [
                (
                    f"keystone_mcp range '{spec}': operator '{item.operator}' is not allowed; "
                    "use >=, < and !="
                )
            ]
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


def _ref_errors(ref: object, path: str) -> list[str]:
    if not isinstance(ref, str) or not _TAG_OR_SHA.match(ref):
        return [f"{path}: ref must be a tag or a 40-character commit"]
    if ref in _BRANCHES:
        return [f"{path}: '{ref}' looks like a branch; use a tag or a commit"]
    return []


def _version_ok(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(_VER, value) is not None


def catalog_errors(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["$: must be an object"]
    errors: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("schema_version: must be 1")
    name = document.get("name")
    if not isinstance(name, str) or not re.fullmatch(_NAME, name):
        errors.append("name: invalid catalog name")
    description = document.get("description")
    if description is not None and (
        not isinstance(description, str)
        or not description
        or "\n" in description
        or len(description) > 200
    ):
        errors.append("description: must be a single line of 1-200 characters")
    packages = document.get("packages")
    if not isinstance(packages, dict) or not packages:
        errors.append("packages: must be an object with at least one package")
        packages = {}
    seen_names: set[str] = set()
    for pkg_name, pkg in packages.items():
        base = f"packages.{pkg_name}"
        if not isinstance(pkg_name, str) or not re.fullmatch(_PKG, pkg_name):
            errors.append(f"{base}: invalid package name")
        if not isinstance(pkg, dict):
            errors.append(f"{base}: must be an object")
            continue
        kind = pkg.get("kind", "service")
        if kind not in {"service", "resource"}:
            errors.append(f"{base}.kind: must be service or resource")
        summary = pkg.get("summary")
        if summary is not None and (
            not isinstance(summary, str) or not summary or "\n" in summary or len(summary) > 200
        ):
            errors.append(f"{base}.summary: must be a single line of 1-200 characters")
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
        if not isinstance(versions, list) or not versions:
            errors.append(f"{base}.versions: at least one version is required")
            versions = []
        seen_versions: set[str] = set()
        for index, item in enumerate(versions):
            vpath = f"{base}.versions[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{vpath}: must be an object")
                continue
            version = item.get("version")
            if not _version_ok(version):
                errors.append(f"{vpath}.version: invalid version")
            elif version in seen_versions:
                errors.append(f"{base}.versions: duplicate version {version}")
            else:
                seen_versions.add(str(version))
            errors.extend(_ref_errors(item.get("ref"), f"{vpath}.ref"))
            compat = item.get("keystone_mcp")
            if kind == "service":
                if not isinstance(compat, str):
                    errors.append(f"{vpath}.keystone_mcp: required for a service")
                else:
                    errors.extend(
                        f"{vpath}.keystone_mcp: {msg}" for msg in keystone_mcp_range_errors(compat)
                    )
            elif compat is not None:
                errors.append(f"{vpath}.keystone_mcp is not allowed for kind: resource")
        seen_names.add(pkg_name)
    bundles = document.get("bundles", {})
    if bundles is None:
        bundles = {}
    if not isinstance(bundles, dict):
        errors.append("bundles: must be an object")
        bundles = {}
    for bundle_name, bundle in bundles.items():
        bpath = f"bundles.{bundle_name}"
        if not isinstance(bundle, dict):
            errors.append(f"{bpath}: must be an object")
            continue
        desc = bundle.get("description")
        if not isinstance(desc, str) or not desc or "\n" in desc or len(desc) > 200:
            errors.append(f"{bpath}.description: must be a single line of 1-200 characters")
        names = bundle.get("packages")
        if not isinstance(names, list) or not names or len(names) > 50:
            errors.append(f"{bpath}.packages: 1-50 package names")
            continue
        if len(set(names)) != len(names):
            errors.append(f"{bpath}.packages: duplicate package")
        for index, pkg_name in enumerate(names):
            if pkg_name not in packages:
                errors.append(f"{bpath}.packages[{index}]: unknown package '{pkg_name}'")
                continue
            kind = (
                packages[pkg_name].get("kind", "service")
                if isinstance(packages[pkg_name], dict)
                else "service"
            )
            if kind == "resource":
                errors.append(
                    f"{bpath}.packages[{index}]: '{pkg_name}' is a resource; "
                    "resources are installed through needs"
                )
    return errors


def sources_errors(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["$: must be an object"]
    errors: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("schema_version: must be 1")
    sources = document.get("sources")
    if not isinstance(sources, list):
        return errors + ["sources: must be a list"]
    names: set[str] = set()
    urls: set[str] = set()
    priorities: set[int] = set()
    for index, item in enumerate(sources):
        path = f"sources[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(_NAME, name):
            errors.append(f"{path}.name: invalid source name")
        elif name in names:
            errors.append(f"sources: two sources share name {name}")
        else:
            names.add(name)
        url_msgs = url_errors(item.get("url"))
        errors.extend(f"{path}.url: {msg}" for msg in url_msgs)
        if isinstance(item.get("url"), str) and item["url"] in urls:
            errors.append(f"sources: two sources share url {item['url']}")
        elif isinstance(item.get("url"), str):
            urls.add(item["url"])
        errors.extend(_ref_errors(item.get("ref"), f"{path}.ref"))
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
            errors.append(f"{path}.priority: must be an integer from 0 to 1000")
        elif priority in priorities:
            errors.append(f"sources: two sources share priority {priority}")
        else:
            priorities.add(priority)
    return errors


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
