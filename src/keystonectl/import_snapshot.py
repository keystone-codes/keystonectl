"""Bring a tool snapshot taken by a running box into its ``local/<name>/`` package.

``creds_snapshot_tools`` in a stdio-box stores a ``tools/list`` snapshot taken under
someone's account and returns its ``id`` and ``sha256``. This command reads that
snapshot from the box container with ``credsctl --json snapshot export <id>``, checks
its hash, writes it as ``local/<name>/tools.snapshot.json``, and updates the package
manifest and policy example in place with the ``package wrap-stdio`` logic. The
snapshot JSON never passes through the agent's arguments. ``service upgrade`` is left
to the operator, because it needs a person's confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from keystonectl.errors import ERRORS
from keystonectl.generate.docker import compose_prefix, daemon_down
from keystonectl.generate.files import atomic_write
from keystonectl.lifecycle import _entry, _layout
from keystonectl.local_packages import is_local_entry, local_root, read_manifest
from keystonectl.lock import LockError, load_lock
from keystonectl.output import Result
from keystonectl.runner import CommandRunner
from keystonectl.wrap_stdio import run_wrap_stdio

COMMAND = "package import-snapshot"
SNAPSHOT_FILE = "tools.snapshot.json"
# Files ``run_wrap_stdio`` may write when it updates a package in place.
_TOUCHED = (SNAPSHOT_FILE, "keystone-service.yaml", "policy.example.yaml")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_EXPORT_TIMEOUT = 120
_STDERR_TAIL = 20


def snapshot_sha256(document: object) -> str:
    """The stdio-box snapshot hash: canonical JSON without ``taken_at``, as UTF-8."""

    if isinstance(document, dict):
        document = {key: value for key, value in document.items() if key != "taken_at"}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_snapshot(
    root: Path,
    name: str,
    *,
    snapshot_id: str,
    sha256: str,
    version: str,
    runner: CommandRunner,
    read_only: Sequence[str] | None = None,
    destructive: Sequence[str] | None = None,
) -> Result:
    """Fetch snapshot ``snapshot_id`` from the package's box and update ``local/<name>/``."""

    root = Path(root)
    try:
        lock = load_lock(root / "services.lock.yaml")
    except LockError as exc:
        return _error(name, exc.error_id, exc.message, hint=exc.hint)
    entry = _entry(lock, name)
    if entry is None:
        return _error(name, "not_local_package", f"{name} is not installed.")
    if not is_local_entry(entry):
        return _error(
            name,
            "not_local_package",
            f"{name} is installed from {entry.get('source')!r}, not from local/.",
        )

    if not snapshot_id.isascii() or not snapshot_id.isdigit():
        return _error(name, "usage", f"--id {snapshot_id!r} is not a snapshot id number.")
    wanted = sha256.lower()
    if _SHA256_RE.fullmatch(wanted) is None:
        return _error(name, "usage", "--sha256 must be 64 hexadecimal characters.")
    if _VERSION_RE.fullmatch(version) is None:
        return _error(name, "usage", f"--version {version!r} is not a semantic version X.Y.Z.")

    manifest, problem = read_manifest(root, name)
    if manifest is None:
        return _error(name, "manifest_invalid", problem or f"local/{name} cannot be read.")
    current = _manifest_version(manifest)
    if current is None:
        return _error(name, "manifest_invalid", f"local/{name}: version is not X.Y.Z.")
    if Version(version) <= Version(current):
        return _error(
            name,
            "usage",
            f"--version {version} must be higher than {current} in local/{name}.",
        )

    service = _box_service(entry, manifest)
    if service is None:
        return _error(
            name,
            "manifest_invalid",
            f"The lock records no container for the MCP role of {name}.",
        )

    fetched = _export(root, lock, service, snapshot_id, runner)
    if isinstance(fetched, Result):
        fetched.target = name
        return fetched
    text, reported = fetched

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return _error(
            name,
            "snapshot_hash_mismatch",
            f"Snapshot {snapshot_id} from the box is not valid JSON.",
        )
    computed = snapshot_sha256(document)
    if computed != wanted or computed != reported:
        return _error(
            name,
            "snapshot_hash_mismatch",
            (
                f"Snapshot {snapshot_id} hashes to {computed}; "
                f"--sha256 is {wanted} and the box reported {reported}."
            ),
        )

    folder = local_root(root) / name
    saved = _save(folder)
    before = _tools(manifest)
    try:
        atomic_write(folder / SNAPSHOT_FILE, text.encode("utf-8"), mode=0o644)
        wrapped = run_wrap_stdio(
            folder,
            image=_image(manifest),
            tools_snapshot=folder / SNAPSHOT_FILE,
            no_discovery=False,
            name=name,
            namespace=_namespace(manifest),
            derived_image=False,
            force=False,
            runner=runner,
            version=version,
            read_only=read_only,
            destructive=destructive,
        )
    except BaseException:
        _restore(folder, saved)
        raise
    if wrapped.code != 0:
        _restore(folder, saved)
        wrapped.command = COMMAND
        wrapped.target = name
        return wrapped

    updated, _problem = read_manifest(root, name)
    after = _tools(updated or {})
    return Result(
        code=0,
        command=COMMAND,
        summary=(
            f"Imported snapshot {snapshot_id} into local/{name}, version {version}. "
            f"Run keystonectl service upgrade {name} to install it."
        ),
        target=name,
        warnings=list(wrapped.warnings),
        changes=wrapped.changes,
        result={
            "package": name,
            "version": version,
            "tools_added": sorted(set(after) - set(before)),
            "tools_removed": sorted(set(before) - set(after)),
            "next": f"keystonectl service upgrade {name}",
        },
    )


def _export(
    root: Path,
    lock: Mapping[str, Any],
    service: str,
    snapshot_id: str,
    runner: CommandRunner,
) -> tuple[str, str] | Result:
    """``credsctl --json snapshot export`` in the box: ``(snapshot text, reported sha256)``."""

    layout = _layout(lock)
    project = str(layout.get("project") or "")
    args = [
        *compose_prefix(root, layout, project=project or None),
        "exec",
        "-T",
        service,
        "credsctl",
        "--json",
        "snapshot",
        "export",
        snapshot_id,
    ]
    try:
        completed = runner.run(args, cwd=root, timeout=_EXPORT_TIMEOUT)
    except OSError:
        return _error("", "docker_unavailable", "Docker is not available.")
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    envelope = _envelope(stdout)
    if completed.returncode != 0:
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        if completed.returncode == 3 and (
            error.get("id") == "snapshot_not_found"
            or (not error and "snapshot_not_found" in f"{stdout}\n{stderr}")
        ):
            return _error(
                "",
                "snapshot_not_found",
                f"The box {service} has no stored snapshot {snapshot_id}.",
            )
        if daemon_down(completed):
            return _error("", "docker_unavailable", "Docker is not available.")
        return _box_failed(service, error.get("message") or stderr or stdout)
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else None
    if (
        result is None
        or not isinstance(result.get("snapshot"), str)
        or not isinstance(result.get("sha256"), str)
    ):
        return _box_failed(service, "credsctl did not print a snapshot export result.")
    return result["snapshot"], result["sha256"].lower()


def _envelope(stdout: str) -> dict[str, Any]:
    try:
        loaded = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _box_failed(service: str, detail: str) -> Result:
    lines = str(detail).strip().splitlines()[-_STDERR_TAIL:]
    tail = "\n".join(lines)
    message = f"credsctl in {service} could not export the snapshot."
    result = _error("", "service_unhealthy", message)
    if tail:
        result.result = {"stderr": tail}
    return result


def _box_service(entry: Mapping[str, Any], manifest: Mapping[str, Any]) -> str | None:
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    role = mcp.get("container")
    containers = entry.get("containers") if isinstance(entry.get("containers"), dict) else {}
    spec = containers.get(role) if isinstance(role, str) else None
    service = spec.get("service") if isinstance(spec, dict) else None
    return service if isinstance(service, str) and service else None


def _image(manifest: Mapping[str, Any]) -> str:
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    spec = containers.get(mcp.get("container")) if isinstance(mcp.get("container"), str) else None
    image = spec.get("image") if isinstance(spec, dict) else None
    return str(image or "")


def _namespace(manifest: Mapping[str, Any]) -> str | None:
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    namespace = mcp.get("namespace")
    return namespace if isinstance(namespace, str) else None


def _tools(manifest: Mapping[str, Any]) -> list[str]:
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    tools = mcp.get("tools") if isinstance(mcp.get("tools"), list) else []
    return [str(item) for item in tools]


def _manifest_version(manifest: Mapping[str, Any]) -> str | None:
    value = manifest.get("version")
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        return None
    try:
        Version(value)
    except InvalidVersion:
        return None
    return value


def _save(folder: Path) -> dict[str, tuple[bytes, int] | None]:
    saved: dict[str, tuple[bytes, int] | None] = {}
    for item in _TOUCHED:
        path = folder / item
        if path.is_file():
            saved[item] = (path.read_bytes(), os.stat(path).st_mode & 0o777)
        else:
            saved[item] = None
    return saved


def _restore(folder: Path, saved: Mapping[str, tuple[bytes, int] | None]) -> None:
    for item, previous in saved.items():
        path = folder / item
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            data, mode = previous
            atomic_write(path, data, mode=mode)


def _error(target: str, error_id: str, message: str, *, hint: str | None = None) -> Result:
    spec = ERRORS[error_id]
    return Result(
        code=spec.code,
        command=COMMAND,
        summary=message,
        target=target or None,
        error={"id": error_id, "message": message, "hint": hint or spec.action},
        retryable=spec.retryable,
    )
