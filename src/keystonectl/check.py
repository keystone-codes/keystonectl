"""``keystonectl check`` reads the deployment and does not use the network."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from keystonectl.compat import detect_keystone_mcp_version
from keystonectl.errors import ERRORS, fragment_modified_message
from keystonectl.keystone_mcp_rules import MAX_REGISTRY_ENTRIES, namespace_errors
from keystonectl.lock import LockError, load_lock, validate_lock
from keystonectl.output import Result

COMPOSE_CANDIDATES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
_EXTRA: list[Callable[..., dict[str, Any]]] = []


class Paths:
    """V1 paths used until ``layout.Layout`` is available."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def compose(self) -> Path:
        for name in COMPOSE_CANDIDATES:
            candidate = self.root / name
            if candidate.is_file():
                return candidate
        return self.root / COMPOSE_CANDIDATES[0]

    @property
    def packages_compose(self) -> Path:
        return self.root / "docker-compose.packages.yml"

    @property
    def packages_dir(self) -> Path:
        return self.root / "packages"

    @property
    def keystonectl_dir(self) -> Path:
        return self.root / "keystonectl"

    @property
    def upstreams(self) -> Path:
        return self.keystonectl_dir / "upstreams.yaml"

    @property
    def snapshot(self) -> Path:
        return self.keystonectl_dir / "catalog-snapshot.yaml"

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def lock(self) -> Path:
        return self.root / "services.lock.yaml"

    @property
    def sources(self) -> Path:
        return self.root / "sources.yaml"

    @property
    def cache(self) -> Path:
        return self.root / ".keystonectl"


def open_paths(root: Path) -> Any:
    try:
        from keystonectl.layout import Layout
    except ImportError:
        return Paths(root)
    return Layout(root)


def register_check(fn: Callable[..., dict[str, Any]]) -> None:
    """Extension point. Set ``fn.needs_docker = True`` to skip when Docker is absent."""
    _EXTRA.append(fn)


def reset_checks() -> None:
    _EXTRA.clear()


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _item(check_id: str, status: str, message: str, hint: str = "") -> dict[str, str]:
    return {"id": check_id, "status": status, "message": message, "hint": hint}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value
    return found


def _services(lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (lock.get("services") or []) if isinstance(item, dict)]


def _mcp_services(lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    found = []
    for service in _services(lock):
        if service.get("managed") not in {"generated", "ejected", "remote"}:
            continue
        if service.get("registry_name"):
            found.append(service)
    return found


def _active_names(snapshot: Any) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    names: set[str] = set()
    for item in snapshot.get("active") or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


@dataclass
class CheckContext:
    root: Path
    paths: Any
    lock: dict[str, Any]
    environ: Mapping[str, str] = field(default_factory=dict)


def _layout_checks(paths: Paths, lock_path: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    required = [
        ("compose", paths.compose, "operator compose file"),
        ("upstreams", paths.upstreams, "keystonectl/upstreams.yaml"),
        ("snapshot", paths.snapshot, "keystonectl/catalog-snapshot.yaml"),
        ("lock", lock_path, "services.lock.yaml"),
        ("packages_compose", paths.packages_compose, "docker-compose.packages.yml"),
    ]
    for check_id, path, label in required:
        if path.is_file():
            checks.append(_item(check_id, "ok", f"{label} is present."))
        else:
            checks.append(_item(check_id, "error", f"{label} is missing.", "Run keystonectl init."))
    if paths.packages_dir.is_dir():
        checks.append(_item("packages", "ok", "packages/ is present."))
    else:
        checks.append(_item("packages", "error", "packages/ is missing.", "Run keystonectl init."))
    return checks


def _registry_checks(paths: Paths, lock: Mapping[str, Any]) -> list[dict[str, str]]:
    document = _load_yaml(paths.upstreams)
    checks: list[dict[str, str]] = []
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        return [_item("registry", "error", "keystonectl/upstreams.yaml has no entries list.")]
    entries = [item for item in document["entries"] if isinstance(item, dict)]
    if len(entries) > MAX_REGISTRY_ENTRIES:
        checks.append(
            _item(
                "registry_size",
                "error",
                f"The registry has {len(entries)} entries; the limit is {MAX_REGISTRY_ENTRIES}.",
            )
        )
    by_name = {item.get("name"): item for item in entries}
    managed_names: set[str] = set()
    for service in _mcp_services(lock):
        registry_name = service.get("registry_name")
        managed_names.add(registry_name)
        entry = by_name.get(registry_name)
        if entry is None:
            checks.append(
                _item(
                    "registry_entry",
                    "error",
                    f"Registry entry {registry_name} is missing.",
                    "Re-run service apply or restore the entry.",
                )
            )
            continue
        auth = entry.get("auth") if isinstance(entry.get("auth"), dict) else {}
        expected = {
            "namespace": service.get("namespace"),
            "url": service.get("url"),
            "bearer_env": _keystone_env(service, "_BEARER"),
            "identity_secret_env": _keystone_env(service, "_IDENTITY_SECRET"),
        }
        actual = {
            "namespace": entry.get("namespace"),
            "url": entry.get("url"),
            "bearer_env": auth.get("bearer_env"),
            "identity_secret_env": auth.get("identity_secret_env"),
        }
        for field_name, want in expected.items():
            if actual[field_name] != want:
                checks.append(
                    _item(
                        "registry_entry",
                        "error",
                        f"Registry entry {registry_name} {field_name} does not match the lock.",
                    )
                )
    for entry in entries:
        if entry.get("name") not in managed_names:
            checks.append(
                _item(
                    "unmanaged_upstream",
                    "warning",
                    f"Upstream {entry.get('name')} is not in the lock and was left unchanged.",
                )
            )
    namespaces = [str(entry.get("namespace")) for entry in entries if entry.get("namespace")]
    for message in namespace_errors(namespaces):
        checks.append(_item("namespace", "error", message, "Rename one of the namespaces."))
    if not any(item["status"] == "error" for item in checks):
        checks.append(_item("registry", "ok", "Registry entries match the lock."))
    return checks


def _keystone_env(service: Mapping[str, Any], suffix: str) -> str | None:
    env = service.get("env") if isinstance(service.get("env"), dict) else {}
    names = env.get("keystone") if isinstance(env.get("keystone"), list) else []
    for name in names:
        if str(name).endswith(suffix):
            return str(name)
    return None


def _snapshot_checks(paths: Paths, lock: Mapping[str, Any]) -> list[dict[str, str]]:
    snapshot = _load_yaml(paths.snapshot)
    active = _active_names(snapshot)
    checks: list[dict[str, str]] = []
    for service in _mcp_services(lock):
        namespace = service.get("namespace")
        for tool in service.get("tools") or []:
            published = f"{namespace}_{tool}"
            if published not in active:
                checks.append(
                    _item(
                        "snapshot",
                        "error",
                        f"{published} is missing from the catalog snapshot active list.",
                    )
                )
    if not checks:
        checks.append(_item("snapshot", "ok", "Snapshot active names cover the lock."))
    return checks


def _fragment_checks(root: Path, lock: Mapping[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    referenced: set[str] = set()
    for service in _services(lock):
        name = str(service.get("name"))
        managed = service.get("managed")
        fragment = service.get("fragment")
        if isinstance(fragment, str):
            referenced.add(fragment)
        if "fragment_sha256" not in service and managed == "generated":
            continue
        if managed == "remote":
            continue
        if managed == "ejected":
            path = (
                root / fragment if isinstance(fragment, str) else root / "packages" / f"{name}.yml"
            )
            if path.is_file():
                checks.append(
                    _item("fragment", "ok", f"{name} is ejected; the fragment is not hashed.")
                )
            else:
                checks.append(
                    _item("fragment", "error", f"Ejected fragment for {name} is missing.")
                )
            continue
        if managed != "generated" or not isinstance(fragment, str):
            continue
        if "fragment_sha256" not in service:
            continue
        path = root / fragment
        expected = service.get("fragment_sha256")
        if not path.is_file() or _sha256(path) != expected:
            checks.append(
                _item(
                    "fragment_modified",
                    "warning",
                    fragment_modified_message(name),
                    "Move your changes into the override file, or eject the service.",
                )
            )
        else:
            checks.append(_item("fragment", "ok", f"{fragment} matches the lock."))
    packages = root / "packages"
    if packages.is_dir():
        for path in sorted(packages.glob("*.yml")):
            if path.name.endswith(".override.yml"):
                continue
            relative = f"packages/{path.name}"
            if relative not in referenced:
                checks.append(
                    _item(
                        "orphan_fragment",
                        "warning",
                        f"{relative} has no lock entry.",
                        "Remove the file or install the package that owns it.",
                    )
                )
    return checks


def _env_checks(root: Path, paths: Paths, lock: Mapping[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    values = _parse_env(paths.env)
    for service in _services(lock):
        env = service.get("env") if isinstance(service.get("env"), dict) else {}
        for name in env.get("keystone") or []:
            if not values.get(str(name), ""):
                checks.append(
                    _item(
                        "env_name",
                        "error",
                        f"{name} is missing or empty in .env.",
                        "Set the name in .env. The value is not printed.",
                    )
                )
        for relative in env.get("files") or []:
            path = root / str(relative)
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                checks.append(
                    _item(
                        "secret_file_unsafe",
                        "error",
                        f"{relative} must be a regular file with mode 600.",
                        "chmod 600",
                    )
                )
    if not any(item["id"] in {"env_name", "secret_file_unsafe"} for item in checks):
        checks.append(_item("env", "ok", "Required environment names are set."))
    return checks


def _compat_checks(
    root: Path, lock: Mapping[str, Any], environ: Mapping[str, str]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    from keystonectl.catalog import is_compatible

    detection = detect_keystone_mcp_version(root, lock=lock, environ=environ)
    checks: list[dict[str, str]] = []
    missing: list[dict[str, Any]] = []
    if detection.question is not None and any(
        isinstance(service.get("compat_keystone_mcp"), str) for service in _services(lock)
    ):
        missing.append(detection.question.as_missing())
        checks.append(
            _item(
                "keystone_mcp_version_unknown",
                "error",
                detection.question.text,
                ERRORS["keystone_mcp_version_unknown"].action,
            )
        )
        return checks, missing
    for service in _services(lock):
        spec = service.get("compat_keystone_mcp")
        if not isinstance(spec, str) or not detection.version:
            continue
        if not is_compatible(spec, detection.version):
            checks.append(
                _item(
                    "incompatible_keystone_mcp",
                    "error",
                    (
                        f"{service.get('name')} requires keystone-mcp {spec}; "
                        f"this deployment runs {detection.version}."
                    ),
                    ERRORS["incompatible_keystone_mcp"].action,
                )
            )
    if not any(item["status"] == "error" for item in checks):
        checks.append(_item("keystone_mcp", "ok", "Installed packages match this keystone-mcp."))
    return checks, missing


def run_check(root: Path, *, environ: Mapping[str, str] | None = None) -> Result:
    from keystonectl.compose_gen import ensure_registered

    ensure_registered()
    command = "check"
    env = dict(environ or {})
    paths = open_paths(root)
    checks = _layout_checks(paths, root / "services.lock.yaml")
    lock_path = root / "services.lock.yaml"
    missing: list[dict[str, Any]] = []
    lock: dict[str, Any] = {}
    if lock_path.is_file():
        try:
            raw = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            checks.append(
                _item("lock_schema", "error", f"services.lock.yaml is not valid YAML: {exc}")
            )
            raw = None
        if isinstance(raw, dict) and raw.get("layout_version") != 1:
            checks.append(
                _item(
                    "layout_unsupported",
                    "error",
                    "layout_version must be 1.",
                    ERRORS["layout_unsupported"].action,
                )
            )
        elif isinstance(raw, dict):
            problems = validate_lock(raw)
            if problems:
                checks.append(
                    _item("lock_schema", "error", problems[0], ERRORS["lock_corrupt"].action)
                )
            else:
                try:
                    lock = load_lock(lock_path)
                except LockError as exc:
                    checks.append(_item("lock_schema", "error", exc.message, exc.hint))
                else:
                    checks.extend(_registry_checks(paths, lock))
                    checks.extend(_snapshot_checks(paths, lock))
                    checks.extend(_fragment_checks(root, lock))
                    checks.extend(_env_checks(root, paths, lock))
                    compat, missing = _compat_checks(root, lock, env)
                    checks.extend(compat)
    context = CheckContext(root=root, paths=paths, lock=lock, environ=env)
    for fn in _EXTRA:
        check_id = getattr(fn, "check_id", getattr(fn, "__name__", "extra"))
        if getattr(fn, "needs_docker", False) and not docker_available():
            checks.append(
                _item(
                    str(check_id),
                    "warning",
                    "Docker is not available; this check was skipped.",
                    "Install Docker to run this check.",
                )
            )
            continue
        checks.append(fn(context))
    errors = [item for item in checks if item["status"] == "error"]
    blocking = [item for item in errors if item["id"] != "keystone_mcp_version_unknown"]
    if missing and not blocking:
        return Result(
            code=2,
            command=command,
            summary="1 answer is missing"
            if len(missing) == 1
            else f"{len(missing)} answers are missing",
            missing=missing,
            result={"checks": checks},
        )
    if errors:
        error_id = (
            "layout_unsupported"
            if [item["id"] for item in errors] == ["layout_unsupported"]
            else "check_failed"
        )
        if (
            any(item["id"] == "layout_unsupported" for item in errors)
            and error_id != "check_failed"
        ):
            error_id = "layout_unsupported"
        if len(errors) == 1 and errors[0]["id"] == "layout_unsupported":
            error_id = "layout_unsupported"
        else:
            error_id = "check_failed"
        spec = ERRORS[error_id]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            error={
                "id": spec.id,
                "message": errors[0]["message"],
                "hint": errors[0]["hint"] or spec.action,
            },
            result={"checks": checks},
        )
    warnings = [
        {"id": item["id"], "message": item["message"]}
        for item in checks
        if item["status"] == "warning"
    ]
    return Result(
        code=0,
        command=command,
        summary="Deployment check passed.",
        warnings=warnings,
        result={"checks": checks},
    )
