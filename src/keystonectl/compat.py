"""keystone-mcp version detection and package compatibility."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from keystonectl.answers import Question
from keystonectl.catalog import is_compatible

_IMAGE_RE = re.compile(
    r"(?:^|/)keystone-mcp:(?P<v>\d+\.\d+\.\d+[0-9A-Za-z.+-]*)"
    r"(?:@sha256:[0-9a-f]{64})?$"
)
_VAR_RE = re.compile(
    r"\$(?:\$|\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:-|-)(?P<default>[^}]*))?\})"
)
VERSION_VAR = "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION"
VERSION_QUESTION = Question(
    VERSION_VAR,
    "Which keystone-mcp version runs here (for example 0.5.0)?",
    "text",
)


@dataclass(frozen=True)
class Detection:
    version: str | None
    image: str | None
    question: Question | None
    via: str


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def expand_compose(text: str, environ: Mapping[str, str], dotenv: Mapping[str, str]) -> str:
    """Expand ``${VAR}``, ``${VAR:-default}``, ``${VAR-default}``, and ``$$``."""

    def replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name = match.group("name")
        if name in environ:
            value: str | None = environ[name]
        elif name in dotenv:
            value = dotenv[name]
        else:
            value = None
        op = match.group("op")
        if value is None or (value == "" and op == ":-"):
            if op in {"-", ":-"}:
                return match.group("default") or ""
            return ""
        return value

    return _VAR_RE.sub(replace, text)


def _compose_keystone_image(root: Path) -> str | None:
    """Return the keystone-mcp image from the operator compose file, when one is present."""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict):
            continue
        for service in services.values():
            if not isinstance(service, dict):
                continue
            image = service.get("image")
            if isinstance(image, str) and "keystone-mcp" in image:
                return image
    return None


def image_version(image: str) -> str | None:
    match = _IMAGE_RE.search(image.strip())
    if not match:
        return None
    return match.group("v")


def detect_keystone_mcp_version(
    root: Path,
    *,
    lock: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    answers: Mapping[str, str] | None = None,
) -> Detection:
    """Read the keystone-mcp version for a deployment root.

    Order: image tag, ``pyproject.toml`` in ``build.context``, then a stored
    answer while the image is unchanged, then the question.
    """
    env = dict(environ or {})
    answered = dict(answers or {})
    document = dict(lock or {})
    layout = document.get("layout") if isinstance(document.get("layout"), dict) else {}
    dotenv = read_dotenv(Path(root) / ".env")
    stored_image = layout.get("keystone_image") if isinstance(layout, dict) else None
    image = _compose_keystone_image(Path(root)) or stored_image
    build = layout.get("keystone_build") if isinstance(layout, dict) else None
    if isinstance(image, str):
        image = expand_compose(image, env, dotenv)
    if isinstance(build, str):
        build = expand_compose(build, env, dotenv)
    if isinstance(image, str):
        found = image_version(image)
        if found:
            return Detection(found, image, None, "image")
    if isinstance(build, str) and build:
        context = Path(build)
        if not context.is_absolute():
            context = Path(root) / context
        pyproject = context / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as handle:
                loaded = tomllib.load(handle)
            version = loaded.get("project", {}).get("version")
            if isinstance(version, str) and version:
                return Detection(version, image if isinstance(image, str) else None, None, "build")
    stored = layout.get("keystone_mcp_version") if isinstance(layout, dict) else None
    if isinstance(stored, str) and stored and stored_image == image:
        return Detection(stored, image if isinstance(image, str) else None, None, "lock")
    given = answered.get(VERSION_VAR) or env.get(VERSION_VAR)
    if given:
        return Detection(given, image if isinstance(image, str) else None, None, "answer")
    return Detection(None, image if isinstance(image, str) else None, VERSION_QUESTION, "ask")


def ranges_differ(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return SpecifierSet(left) != SpecifierSet(right)
    except ValueError:
        return left != right


def catalog_field_mismatches(manifest: Mapping[str, Any], entry: Mapping[str, Any]) -> list[str]:
    """Field names where the manifest disagrees with the catalog entry."""
    mismatches: list[str] = []
    for field in ("name", "kind", "version"):
        if manifest.get(field) != entry.get(field):
            mismatches.append(field)
    manifest_provides = manifest.get("provides")
    entry_provides = entry.get("provides")
    if entry.get("kind") == "resource" or isinstance(entry_provides, dict):
        left = manifest_provides if isinstance(manifest_provides, dict) else {}
        right = entry_provides if isinstance(entry_provides, dict) else {}
        if left.get("kind") != right.get("kind"):
            mismatches.append("provides.kind")
        if left.get("version") != right.get("version"):
            mismatches.append("provides.version")
    return mismatches


def missing_requires(manifest: Mapping[str, Any], lock: Mapping[str, Any]) -> list[str]:
    installed = {
        item.get("name") for item in (lock.get("services") or []) if isinstance(item, dict)
    }
    installed |= {
        item.get("package")
        for item in (lock.get("services") or [])
        if isinstance(item, dict) and item.get("package")
    }
    missing: list[str] = []
    for name in manifest.get("requires") or []:
        if name not in installed:
            missing.append(str(name))
    return missing


def compatible_with(spec: str | None, version: str | None) -> bool:
    if not spec or not version:
        return spec is None
    return is_compatible(spec, version)


def version_sort_key(version: str) -> Version:
    return Version(version)
