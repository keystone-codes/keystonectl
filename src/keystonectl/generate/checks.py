"""``keystonectl check`` additions for Compose version and operator overrides."""

from __future__ import annotations

import subprocess
from typing import Any

from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.generate.packages_file import compose_is_old


def read_compose_version() -> tuple[int, str]:
    """Return ``(returncode, stdout)`` of ``docker compose version --short``."""

    try:
        completed = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return completed.returncode, completed.stdout


def read_compose_config(root: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.packages.yml",
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return completed.returncode, completed.stdout


def _item(check_id: str, status: str, message: str, hint: str = "") -> dict[str, str]:
    return {"id": check_id, "status": status, "message": message, "hint": hint}


def check_compose_version(_context: Any) -> dict[str, str]:
    code, text = read_compose_version()
    if code != 0 or not text.strip():
        return _item(
            "compose_version",
            "warning",
            "Docker Compose version could not be read.",
            "Install the Docker Compose plugin 2.24 or newer.",
        )
    if compose_is_old(text):
        spec = ERRORS["compose_too_old"]
        return _item("compose_version", "error", spec.title, spec.action)
    return _item("compose_version", "ok", f"Docker Compose {text.strip()} is new enough.")


def check_override_included(context: Any) -> dict[str, str]:
    root = context.root
    packages = root / "packages"
    missing: list[str] = []
    if packages.is_dir():
        packages_file = (
            (root / "docker-compose.packages.yml").read_text(encoding="utf-8")
            if (root / "docker-compose.packages.yml").is_file()
            else ""
        )
        for path in sorted(packages.glob("*.override.yml")):
            name = path.name.removesuffix(".override.yml")
            if f"packages/{name}.override.yml" not in packages_file:
                missing.append(name)
    if missing:
        spec = WARNINGS["override_not_included"]
        return _item("override_not_included", "warning", spec.title, spec.action)
    return _item("override_included", "ok", "Operator overrides are included when they exist.")


def check_override_access(context: Any) -> dict[str, str]:
    import json

    code, text = read_compose_config(str(context.root))
    if code != 0 or not text.strip():
        return _item(
            "override_access",
            "warning",
            "docker compose config did not return JSON, so override access was not checked.",
            "Fix the compose files and run keystonectl check again.",
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return _item(
            "override_access",
            "warning",
            "docker compose config did not return JSON, so override access was not checked.",
            "Fix the compose files and run keystonectl check again.",
        )
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return _item(
            "override_access",
            "warning",
            "docker compose config did not return JSON, so override access was not checked.",
            "Fix the compose files and run keystonectl check again.",
        )
    if not services:
        return _item("override_access", "ok", "Compose config has no services to compare.")
    widened = _widened(context.root, services)
    if widened:
        spec = WARNINGS["override_widens_access"]
        listed = ", ".join(f"{service} {field}" for service, field in widened)
        return _item(
            "override_widens_access",
            "warning",
            f"{spec.title} ({listed}).",
            spec.action,
        )
    return _item("override_access", "ok", "Overrides do not publish ports or add privileges.")


def _package_names(root: Any) -> set[str]:
    packages = root / "packages"
    names: set[str] = set()
    if packages.is_dir():
        for path in packages.glob("*.yml"):
            if path.name.endswith(".override.yml"):
                continue
            names.add(path.stem)
    return names


def _port_leaves_loopback(port: object) -> bool:
    """A port bound only to 127.0.0.1 is the loopback publish from K17."""

    if isinstance(port, str):
        body = port.split("/", 1)[0]
        return not body.startswith("127.0.0.1:")
    if isinstance(port, dict):
        published = port.get("published")
        host_ip = port.get("host_ip")
        if published in (None, "") and host_ip in (None, ""):
            return False
        return str(host_ip or "") != "127.0.0.1"
    return False


def _volume_leaves_package(volume: object) -> bool:
    if isinstance(volume, str):
        if "docker.sock" in volume:
            return True
        source = volume.split(":", 1)[0]
        return source.startswith(("/", ".", "~"))
    if not isinstance(volume, dict):
        return False
    source = str(volume.get("source") or "")
    target = str(volume.get("target") or "")
    if "docker.sock" in source or "docker.sock" in target:
        return True
    if volume.get("type") == "bind":
        return True
    return source.startswith(("/", ".", "~"))


def _widened(root: Any, services: dict[str, Any]) -> list[tuple[str, str]]:
    names = _package_names(root)
    found: list[tuple[str, str]] = []
    for name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        service = str(name)
        if not any(service.startswith(f"{package}-") for package in names):
            continue
        fields: list[str] = []
        if spec.get("privileged"):
            fields.append("privileged")
        if spec.get("cap_add"):
            fields.append("cap_add")
        if isinstance(spec.get("network_mode"), str) and spec.get("network_mode"):
            fields.append("network_mode")
        if spec.get("pid") == "host":
            fields.append("pid")
        if any(_port_leaves_loopback(port) for port in spec.get("ports") or []):
            fields.append("ports")
        if any(_volume_leaves_package(volume) for volume in spec.get("volumes") or []):
            fields.append("volumes")
        for field in fields:
            found.append((service, field))
    return found


def ensure_checks() -> None:
    from keystonectl.check import _EXTRA, register_check

    present = {getattr(fn, "check_id", None) for fn in _EXTRA}
    check_compose_version.needs_docker = True  # type: ignore[attr-defined]
    check_compose_version.check_id = "compose_version"  # type: ignore[attr-defined]
    check_override_access.needs_docker = True  # type: ignore[attr-defined]
    check_override_access.check_id = "override_access"  # type: ignore[attr-defined]
    check_override_included.check_id = "override_included"  # type: ignore[attr-defined]
    if "compose_version" not in present:
        register_check(check_compose_version)
    if "override_included" not in present:
        register_check(check_override_included)
    if "override_access" not in present:
        register_check(check_override_access)
