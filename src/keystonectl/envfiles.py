"""Read and write package env files for Docker Compose and bash."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9_./:@+=,%-]*\Z")
_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "$": "$",
    "`": "`",
}


def quote_env(value: str) -> str:
    """Quote a value so Compose env_file and ``set -a; . file`` agree."""
    if "\n" in value or "\r" in value:
        raise ValueError("env values cannot contain a newline")
    if _SAFE.fullmatch(value):
        return value
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    )
    return f'"{escaped}"'


def _unescape(body: str) -> str:
    chars: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            chars.append(_ESCAPES.get(nxt, "\\" + nxt))
            index += 2
            continue
        chars.append(char)
        index += 1
    return "".join(chars)


def _parse_assignment(line: str) -> tuple[str, str] | None:
    body = line.strip()
    if not body or body.startswith("#"):
        return None
    if body.startswith("export "):
        body = body[len("export ") :].strip()
    if "=" not in body:
        return None
    key, raw = body.split("=", 1)
    key = key.strip()
    if not key:
        return None
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return key, _unescape(raw[1:-1])
    return key, raw


def read_env(path: Path) -> dict[str, str]:
    """Return assignments from ``path``. A missing file is an empty mapping."""
    path = Path(path)
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_assignment(line)
        if parsed is not None:
            found[parsed[0]] = parsed[1]
    return found


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.chmod(temporary, 0o600)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def upsert_env(
    path: Path,
    values: Mapping[str, str],
    *,
    remove: Iterable[str] = (),
) -> None:
    """Replace named keys, keep every other line, and write the file as mode 600."""
    path = Path(path)
    rendered = {key: quote_env(value) for key, value in values.items()}
    drop = set(remove)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines(keepends=True)
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed is None:
            kept.append(line)
            continue
        key = parsed[0]
        if key in drop:
            continue
        if key in rendered:
            if key in seen:
                continue
            seen.add(key)
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            kept.append(f"{key}={rendered[key]}{newline}")
            continue
        kept.append(line)
    for key, quoted in rendered.items():
        if key in seen or key in drop:
            continue
        if kept and not kept[-1].endswith("\n"):
            kept[-1] = kept[-1] + "\n"
        kept.append(f"{key}={quoted}\n")
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write(path, text.encode("utf-8"))
