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
            "ok",
            "Compose config was not available; override access was not compared.",
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return _item("override_access", "ok", "Compose config was not JSON.")
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        return _item("override_access", "ok", "Compose config has no services to compare.")
    widened = _widened(context.root, services)
    if widened:
        spec = WARNINGS["override_widens_access"]
        return _item("override_widens_access", "warning", spec.title, spec.action)
    return _item("override_access", "ok", "Overrides do not publish ports or add privileges.")


def _widened(root: Any, services: dict[str, Any]) -> bool:
    packages = root / "packages"
    names: set[str] = set()
    if packages.is_dir():
        for path in packages.glob("*.yml"):
            if path.name.endswith(".override.yml"):
                continue
            names.add(path.stem)
    for name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        if not any(str(name).startswith(f"{package}-") for package in names):
            continue
        if spec.get("privileged") or spec.get("cap_add"):
            return True
        for port in spec.get("ports") or []:
            published = ""
            if isinstance(port, str):
                published = port
            elif isinstance(port, dict):
                published = str(port.get("published") or port.get("host_ip") or "")
            if published and not str(published).startswith("127.0.0.1"):
                return True
    return False


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
