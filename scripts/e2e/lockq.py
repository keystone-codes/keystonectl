"""Fixed fields from services.lock.yaml. No expressions and no secret values."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def load_lock(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("lock root must be a mapping")
    return document


def _service(document: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for item in document.get("services") or []:
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    return None


def _resource(document: Mapping[str, Any], kind: str) -> Mapping[str, Any] | None:
    for item in document.get("resources") or []:
        if isinstance(item, Mapping) and item.get("kind") == kind:
            return item
    return None


def query_lines(document: Mapping[str, Any], query: str, args: list[str]) -> list[str]:
    layout = document.get("layout") if isinstance(document.get("layout"), Mapping) else {}
    if query == "layout-keystone-service":
        return [str(layout.get("keystone_service") or "")]
    if query == "layout-version":
        return [str(document.get("layout_version") or "")]
    if query == "layout-flavor":
        return [str(layout.get("flavor") or "")]
    if query == "resource":
        if len(args) != 2:
            raise ValueError("resource needs KIND and FIELD")
        resource = _resource(document, args[0])
        if resource is None:
            return []
        value = resource.get(args[1])
        return [] if value is None else [str(value)]
    if query == "service":
        if len(args) != 2:
            raise ValueError("service needs NAME and FIELD")
        service = _service(document, args[0])
        if service is None:
            return ["missing"]
        value = service.get(args[1])
        return [] if value is None else [str(value)]
    if query == "env-files":
        if len(args) != 1:
            raise ValueError("env-files needs a package name")
        service = _service(document, args[0])
        if service is None:
            return []
        env = service.get("env") if isinstance(service.get("env"), Mapping) else {}
        files = env.get("files") if isinstance(env, Mapping) else None
        if not isinstance(files, list):
            return []
        return [str(item) for item in files]
    raise ValueError(f"unknown query {query}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(
            "usage: lockq.py LOCK QUERY [ARG ...]\n"
            "queries: layout-flavor layout-keystone-service layout-version resource service env-files",
            file=sys.stderr,
        )
        return 2
    try:
        document = load_lock(Path(args[0]))
        lines = query_lines(document, args[1], args[2:])
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
