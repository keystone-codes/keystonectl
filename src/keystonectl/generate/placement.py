"""Which env file each container receives.

Three kinds from the layout stay: the shared package file, the MCP file, and
one file per resource kind when that resource is limited to some roles.

A generated or required secret declared by only some roles does not go into
the shared file. Containers that declare the same name share one value and
are the only ones that receive it. The bearer and the assertion secret are
written only to the MCP file (and to the deployment ``.env`` for keystone-mcp).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecretSpec:
    name: str
    roles: tuple[str, ...]
    generate_bytes: int | None
    required: bool
    file: str


@dataclass
class Placement:
    """Env files for one package. Paths are relative to the deployment root."""

    shared: str
    mcp: str | None
    kind_files: dict[str, str]
    secrets: tuple[SecretSpec, ...]
    role_files: dict[str, tuple[str, ...]]
    files: tuple[str, ...] = field(default_factory=tuple)

    def fragment_env_files(self, role: str) -> list[str]:
        return [f"./{path.split('/', 1)[1]}" for path in self.role_files.get(role, ())]


def package_roles(manifest: Mapping[str, Any]) -> list[str]:
    containers = manifest.get("containers")
    if not isinstance(containers, dict):
        return []
    return [str(role) for role, spec in containers.items() if isinstance(spec, dict)]


def mcp_role(manifest: Mapping[str, Any]) -> str | None:
    mcp = manifest.get("mcp")
    if not isinstance(mcp, dict) or not isinstance(mcp.get("container"), str):
        return None
    return str(mcp["container"])


def public_role(manifest: Mapping[str, Any]) -> str | None:
    paths = manifest.get("public_paths")
    if not isinstance(paths, list) or not paths:
        return None
    named = manifest.get("public_container")
    if isinstance(named, str) and named:
        return named
    return mcp_role(manifest)


def resource_roles(manifest: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Kind → roles that join ``keystone-resources`` and receive that kind file.

    A need without ``containers`` applies to every role. A need that lists
    every role is not partial: its ``bind_env`` stays in the shared file.
    """

    roles = package_roles(manifest)
    if not roles:
        return {}
    needs = manifest.get("needs") if isinstance(manifest.get("needs"), list) else []
    found: dict[str, tuple[str, ...]] = {}
    for need in needs:
        if not isinstance(need, dict) or not isinstance(need.get("kind"), str):
            continue
        kind = str(need["kind"])
        listed = need.get("containers")
        if isinstance(listed, list) and listed and set(map(str, listed)) != set(roles):
            ordered = tuple(role for role in roles if role in set(map(str, listed)))
        else:
            ordered = tuple(roles)
        found[kind] = ordered
    return found


def partial_kind_files(manifest: Mapping[str, Any]) -> dict[str, str]:
    name = str(manifest["name"])
    roles = set(package_roles(manifest))
    files: dict[str, str] = {}
    for kind, receivers in resource_roles(manifest).items():
        if set(receivers) != roles:
            files[kind] = f"packages/{name}.{kind}.env"
    return files


def _secret_file(
    manifest: Mapping[str, Any],
    roles: tuple[str, ...],
    *,
    all_roles: set[str],
    mcp: str | None,
) -> str:
    name = str(manifest["name"])
    if set(roles) == all_roles:
        return f"packages/{name}.env"
    if mcp is not None and set(roles) == {mcp}:
        return f"packages/{name}.mcp.env"
    joined = "-".join(sorted(set(roles)))
    return f"packages/{name}.{joined}.env"


def placement(manifest: Mapping[str, Any]) -> Placement:
    name = str(manifest["name"])
    roles = package_roles(manifest)
    all_roles = set(roles)
    mcp = mcp_role(manifest)
    shared = f"packages/{name}.env"
    mcp_file = f"packages/{name}.mcp.env" if mcp else None
    kinds = partial_kind_files(manifest)
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    grouped: dict[str, list[str]] = {}
    meta: dict[str, tuple[int | None, bool]] = {}
    for role in roles:
        spec = containers.get(role)
        if not isinstance(spec, dict):
            continue
        for item in spec.get("secrets") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            secret_name = str(item["name"])
            grouped.setdefault(secret_name, [])
            if role not in grouped[secret_name]:
                grouped[secret_name].append(role)
            generate = item.get("generate") if isinstance(item.get("generate"), dict) else None
            nbytes = int(generate["bytes"]) if generate and "bytes" in generate else None
            required = bool(item.get("required"))
            previous = meta.get(secret_name)
            if previous is None:
                meta[secret_name] = (nbytes, required)
            elif previous[0] != nbytes or previous[1] != required:
                meta[secret_name] = (None, required)
    secrets: list[SecretSpec] = []
    for secret_name, secret_roles in grouped.items():
        ordered = tuple(role for role in roles if role in secret_roles)
        nbytes, required = meta[secret_name]
        secrets.append(
            SecretSpec(
                name=secret_name,
                roles=ordered,
                generate_bytes=nbytes if not required else None,
                required=required,
                file=_secret_file(manifest, ordered, all_roles=all_roles, mcp=mcp),
            )
        )
    role_files: dict[str, tuple[str, ...]] = {}
    for role in roles:
        files: list[str] = [shared]
        for kind, path in kinds.items():
            if role in resource_roles(manifest)[kind]:
                files.append(path)
        if mcp_file and role == mcp:
            files.append(mcp_file)
        for secret in secrets:
            if role in secret.roles and secret.file not in files:
                files.append(secret.file)
        role_files[role] = tuple(files)
    ordered_files: list[str] = [shared]
    if mcp_file:
        ordered_files.append(mcp_file)
    for path in kinds.values():
        if path not in ordered_files:
            ordered_files.append(path)
    for secret in secrets:
        if secret.file not in ordered_files:
            ordered_files.append(secret.file)
    return Placement(
        shared=shared,
        mcp=mcp_file,
        kind_files=kinds,
        secrets=tuple(secrets),
        role_files=role_files,
        files=tuple(ordered_files),
    )


def joins_resources(manifest: Mapping[str, Any], role: str) -> bool:
    provides = manifest.get("provides")
    if isinstance(provides, dict) and provides.get("container") == role:
        return True
    for receivers in resource_roles(manifest).values():
        if role in receivers:
            return True
    return False


def joins_upstreams(manifest: Mapping[str, Any], role: str) -> bool:
    return mcp_role(manifest) == role
