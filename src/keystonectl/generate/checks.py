"""``keystonectl check`` additions for Compose version and operator overrides."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.generate.docker import compose_prefix
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


def read_compose_config(root: str, layout: Mapping[str, Any] | None = None) -> tuple[int, str]:
    root_path = Path(root)
    if layout is None:
        layout = {}
    command = [*compose_prefix(root_path, layout), "config", "--format", "json"]
    try:
        completed = subprocess.run(
            command,
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
            "compose_version_unknown",
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

    lock = getattr(context, "lock", None)
    layout = lock.get("layout") if isinstance(lock, dict) else None
    if not isinstance(layout, dict):
        layout = {}
    code, text = read_compose_config(str(context.root), layout)
    unchecked = _item(
        "override_unchecked",
        "warning",
        "docker compose config did not return JSON, so override access was not checked.",
        "Fix the compose files and run keystonectl check again.",
    )
    if code != 0 or not text.strip():
        return unchecked
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return unchecked
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return unchecked
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


def _compose_container_id(prefix: list[str], service: str, *, cwd: str) -> str:
    try:
        completed = subprocess.run(
            [*prefix, "ps", "-q", service],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[0] if lines else ""


def check_keystone_wiring(context: Any) -> dict[str, str] | None:
    """A running keystone-mcp started with fewer ``-f`` files than the layout records
    sees no upstream at all (empty registry, no ``keystone-mcp-upstreams`` network),
    while every file on disk still looks correct: ``docker-compose.packages.yml`` is
    present and its content is right, so the file-based checks stay green. Compare
    the *running* container's own compose labels and networks instead.
    """
    import json

    lock = getattr(context, "lock", None)
    layout = lock.get("layout") if isinstance(lock, dict) else None
    if not isinstance(layout, dict):
        return None
    compose_files_list = layout.get("compose_files")
    if not isinstance(compose_files_list, list) or len(compose_files_list) < 2:
        return None
    service = str(layout.get("keystone_service") or "")
    if not service:
        return None
    network = layout.get("upstreams_network")
    network_name = str(network.get("name") or "") if isinstance(network, dict) else ""
    prefix = compose_prefix(context.root, layout)
    container_id = _compose_container_id(prefix, service, cwd=str(context.root))
    if not container_id:
        return None
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inspected.returncode != 0 or not inspected.stdout.strip():
        return None
    try:
        document = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        return None
    info = document[0]
    config = info.get("Config") if isinstance(info.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    config_files = str(labels.get("com.docker.compose.project.config_files") or "")
    configured_names = {Path(item).name for item in config_files.split(",") if item.strip()}
    expected_names = {Path(str(item)).name for item in compose_files_list}
    missing_files = sorted(expected_names - configured_names)
    network_settings = (
        info.get("NetworkSettings") if isinstance(info.get("NetworkSettings"), dict) else {}
    )
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings.get("Networks"), dict)
        else {}
    )
    missing_network = bool(network_name) and network_name not in networks
    if missing_files or missing_network:
        reasons = [*missing_files]
        if missing_network:
            reasons.append(f"network {network_name}")
        command = " ".join([*prefix, "up", "-d", "--force-recreate", "--no-deps", service])
        return _item(
            "keystone_wiring_stale",
            "warning",
            f"{service} is running without {', '.join(reasons)}; it cannot see any upstream service.",
            f"Run `{command}` to reconnect it.",
        )
    return _item(
        "keystone_wiring",
        "ok",
        f"{service} is running with every compose file and the upstreams network.",
    )


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
    check_keystone_wiring.needs_docker = True  # type: ignore[attr-defined]
    check_keystone_wiring.check_id = "keystone_wiring"  # type: ignore[attr-defined]
    if "compose_version" not in present:
        register_check(check_compose_version)
    if "override_included" not in present:
        register_check(check_override_included)
    if "override_access" not in present:
        register_check(check_override_access)
    if "keystone_wiring" not in present:
        register_check(check_keystone_wiring)
