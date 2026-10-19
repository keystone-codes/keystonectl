"""Read and write ``services.lock.yaml`` with a lock file, a backup, and an atomic replace."""

from __future__ import annotations

import contextvars
import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from keystonectl import __version__
from keystonectl.errors import ERRORS

_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar("keystonectl_lock_held", default=False)
_ORIGIN: dict[int, bytes] = {}

TOP_LEVEL = {
    "schema_version",
    "layout_version",
    "updated_at",
    "updated_by",
    "layout",
    "services",
    "resources",
}
SERVICE_KEYS = {
    "name",
    "role",
    "managed",
    "source",
    "package",
    "version",
    "ref",
    "commit",
    "manifest_sha256",
    "content_sha256",
    "compat_keystone_mcp",
    "containers",
    "fragment",
    "fragment_sha256",
    "registry_name",
    "namespace",
    "url",
    "tools",
    "filters",
    "env",
    "answers",
    "resources",
    "public",
    "retired_tools",
    "previous",
    "installed_at",
    "updated_at",
}


class LockError(Exception):
    def __init__(self, error_id: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message
        spec = ERRORS[error_id]
        self.hint = hint if hint is not None else spec.action
        self.code = spec.code
        self.retryable = spec.retryable


def utc_now(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_lock(now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "layout_version": 1,
        "updated_at": utc_now(now),
        "updated_by": f"keystonectl {__version__}",
        "layout": None,
        "services": [],
        "resources": [],
    }


def _schema() -> dict[str, Any] | None:
    try:
        from keystonectl.resources import schema_text

        return json.loads(schema_text("lock-v1.json"))
    except (FileNotFoundError, OSError, ModuleNotFoundError, TypeError):
        return None


def _jsonschema_errors(schema: dict[str, Any], document: object) -> list[str]:
    validator = Draft202012Validator(schema)
    messages: list[str] = []
    for error in validator.iter_errors(document):
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        messages.append(f"{path}: {error.message}")
    return messages


def _structural(document: object) -> list[str]:
    """Used until the layout card's lock schema is present."""
    if not isinstance(document, dict):
        return ["$: must be an object"]
    errors: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("schema_version: must be 1")
    if document.get("layout_version") != 1:
        errors.append("layout_version: this keystonectl supports layout 1")
    for key in document:
        if key not in TOP_LEVEL:
            errors.append(f"{key}: unknown field")
    services = document.get("services", [])
    if not isinstance(services, list):
        errors.append("services: must be a list")
        services = []
    for index, service in enumerate(services):
        if not isinstance(service, dict):
            errors.append(f"services[{index}]: must be an object")
            continue
        for key in service:
            if key not in SERVICE_KEYS:
                errors.append(f"services[{index}].{key}: unknown field")
    return errors


def invalid_lock_id(document: object, errors: list[str]) -> str:
    """layout_version other than 1 is layout_unsupported; every other failure is lock_corrupt."""
    layout_other = isinstance(document, dict) and document.get("layout_version") != 1
    if layout_other and any("layout_version" in item for item in errors):
        return "layout_unsupported"
    return "lock_corrupt"


def validate_lock(document: object) -> list[str]:
    schema = _schema()
    if schema is not None:
        errors = _jsonschema_errors(schema, document)
    else:
        errors = _structural(document)
    try:
        from keystonectl.layout import lock_errors
    except ImportError:
        lock_errors = None
    if lock_errors is not None:
        errors.extend(lock_errors(document))
    unique: list[str] = []
    seen: set[str] = set()
    for item in errors:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def load_lock(path: Path) -> dict[str, Any]:
    """Return the lock document. A missing file is an empty state."""
    path = Path(path)
    if not path.is_file():
        return empty_lock()
    raw = path.read_bytes()
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise LockError(
            "lock_corrupt",
            f"{path.name} is not valid YAML: {exc}",
            "Restore services.lock.yaml.bak.",
        ) from exc
    errors = validate_lock(document)
    if errors:
        raise LockError(
            invalid_lock_id(document, errors), errors[0], "Restore services.lock.yaml.bak."
        )
    assert isinstance(document, dict)
    _ORIGIN[id(document)] = raw
    return document


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    """Hold ``services.lock.yaml.lck`` for the whole state-changing command."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lck = Path(str(path) + ".lck")
    fd = os.open(lck, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise LockError("busy", ERRORS["busy"].title, ERRORS["busy"].action) from exc
    token = _HELD.set(True)
    try:
        yield
    finally:
        _HELD.reset(token)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_lock(path: Path, document: dict[str, Any], *, now: datetime | None = None) -> None:
    path = Path(path)
    payload = dict(document)
    if now is not None:
        payload["updated_at"] = utc_now(now)
    errors = validate_lock(payload)
    if errors:
        raise LockError(
            invalid_lock_id(payload, errors), errors[0], "Restore services.lock.yaml.bak."
        )
    original = _ORIGIN.get(id(document))
    if (
        original is not None
        and now is None
        and yaml.safe_load(original.decode("utf-8")) == document
    ):
        data = original
    else:
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        data = text.encode("utf-8")

    def write() -> None:
        if path.is_file():
            backup = Path(str(path) + ".bak")
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
        _atomic_write(path, data, 0o600)

    if _HELD.get():
        write()
        return
    with hold_lock(path):
        write()
