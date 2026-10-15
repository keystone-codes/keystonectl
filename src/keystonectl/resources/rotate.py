"""Replace one catalog consumer's bucket key or database password."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from keystonectl.answers import pkg_var
from keystonectl.catalog import CatalogError, load_sources
from keystonectl.envfiles import read_env, upsert_env
from keystonectl.errors import ERRORS
from keystonectl.generate.docker import compose_prefix, daemon_down, run
from keystonectl.generate.install import _wait
from keystonectl.generate.placement import package_roles, placement
from keystonectl.lifecycle import _confirm, _entry, _error, _layout, _load_lock, _manifest_at
from keystonectl.output import Result, SecretRegistry
from keystonectl.plan import Context
from keystonectl.resources import INTERFACES, binding
from keystonectl.resources.provision import (
    ProvisionResult,
    consumer_ident,
    consumer_secret_path,
    existing_consumer_secret,
    run_provision,
)
from keystonectl.runner import CommandRunner
from keystonectl.sources import load_cached_catalogs, package_cache

_COMMAND = "service rotate-secret"


def rotate_secret(
    root: Path,
    name: str,
    kind: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock: Callable[[], datetime],
) -> Result:
    """Rotate a catalog consumer secret, then recreate the containers that use it."""
    lock = _load_lock(root)
    entry = _entry(lock, name)
    if entry is None:
        return _error(_COMMAND, name, "package_not_found", f"{name} is not installed.")
    listed = entry.get("resources") if isinstance(entry.get("resources"), list) else []
    if kind not in listed:
        return _error(_COMMAND, name, "resource_not_bound", f"{name} is not bound to {kind}.")
    resource = binding._resource(lock, kind)
    consumer = binding._consumer(resource, name) if isinstance(resource, dict) else None
    if not isinstance(resource, dict) or consumer is None:
        return _error(_COMMAND, name, "resource_not_bound", f"{name} is not bound to {kind}.")
    if resource.get("origin") == "external":
        # Apply leaves a complete env file in place, so a new FILE answer does not
        # replace an external secret. The operator removes the service and installs it again.
        return _error(
            _COMMAND,
            name,
            "rotate_external_unsupported",
            f"{name} uses an external {kind}. keystonectl cannot change that key.",
        )
    provider = resource.get("provider")
    if resource.get("origin") != "catalog" or not isinstance(provider, str) or not provider:
        return _error(
            _COMMAND,
            name,
            "no_provider",
            f"No catalog provider is recorded for {kind}.",
        )
    blocked = _confirm(
        command=_COMMAND,
        target=name,
        var=pkg_var(name, "CONFIRM_ROTATE"),
        question=(
            f"Rotate the {kind} secret of {name}? Its containers restart; "
            "anything else that uses the old secret stops working."
        ),
        sets=sets,
        environ=environ,
        interactive=interactive,
    )
    if blocked is not None:
        return blocked
    return _rotate_catalog(
        root,
        lock,
        entry,
        name,
        kind,
        provider,
        runner=runner,
        secrets=secrets,
        sets=sets,
        environ=environ,
        interactive=interactive,
        clock=clock,
    )


def _rotate_catalog(
    root: Path,
    lock: dict[str, Any],
    entry: Mapping[str, Any],
    name: str,
    kind: str,
    provider: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock: Callable[[], datetime],
) -> Result:
    manifest = _manifest_at(root, entry)
    if manifest is None:
        return _error(_COMMAND, name, "manifest_invalid", f"{name} has no cached manifest.")
    need = _need(manifest, kind)
    if need is None:
        return _error(_COMMAND, name, "resource_not_bound", f"{name} is not bound to {kind}.")
    provider_entry = _entry(lock, provider)
    if provider_entry is None:
        return _error(_COMMAND, name, "no_provider", f"Provider {provider} is not installed.")
    provider_manifest = _manifest_at(root, provider_entry)
    if provider_manifest is None:
        return _error(_COMMAND, name, "manifest_invalid", f"{provider} has no cached manifest.")
    provision = provider_manifest.get("provision")
    if not isinstance(provision, dict):
        return _error(_COMMAND, name, "manifest_invalid", f"{provider} has no provision script.")
    image = provision.get("image")
    script = provision.get("script")
    if not isinstance(image, str) or not isinstance(script, str):
        return _error(_COMMAND, name, "manifest_invalid", f"{provider} has no provision script.")

    ctx = Context(
        root=root,
        runner=runner,
        secrets=secrets,
        sets=dict(sets),
        environ=environ,
        interactive=interactive,
        lock=lock,
        clock=clock,
    )
    try:
        ctx.sources = load_sources(root / "sources.yaml")
        ctx.catalogs = load_cached_catalogs(root, ctx.sources)
    except CatalogError as exc:
        return _error(_COMMAND, name, "catalog_invalid", str(exc))

    package = str(manifest["name"])
    bind_env = need.get("bind_env") if isinstance(need.get("bind_env"), dict) else {}
    env_path = root / binding._env_relpath(package, manifest, need)
    secret_path = consumer_secret_path(root, package, kind)
    _register_saved(secrets, kind, bind_env, env_path, secret_path)
    old = existing_consumer_secret(
        binding._env_with_saved_secret(root, package, manifest, need, kind),
        bind_env,
    )
    if not old:
        return _error(
            _COMMAND,
            name,
            "rotate_failed",
            "No saved secret was found for this service.",
            hint=f"Run `service apply {name}`, then rotate again.",
        )
    secrets.register(old)

    source = str(provider_entry.get("source") or "")
    version = str(provider_entry.get("version") or "")
    package_dir = package_cache(root, source, provider, version)
    catalog_entry = binding._catalog_entry(ctx, source, provider)
    if isinstance(catalog_entry, dict):
        resolved = binding._resolved_provider(source, provider, catalog_entry)
        if resolved.path != ".":
            package_dir = package_dir / resolved.path
    try:
        host, port = binding._container_address(provider_manifest)
        admin = binding._admin_values(ctx, provider_manifest)
    except (KeyError, TypeError, ValueError):
        return _error(_COMMAND, name, "manifest_invalid", f"{provider} has no container address.")

    ident = consumer_ident(package) if kind == "postgres" else package
    extensions = binding._extensions(need)
    saved = _snapshot([env_path, secret_path])
    new = token_hex(32)
    secrets.register(new)
    first = _provision(
        runner,
        secret=new,
        root=root,
        kind=kind,
        image=image,
        script=script,
        package_dir=package_dir,
        consumer=package,
        admin=admin,
        secrets=secrets,
        host=host,
        port=port,
        database=ident,
        extensions=extensions,
    )
    if not first.ok:
        return _provision_error(name, first)

    values = _consumer_values(kind, first, host=host, port=port, ident=ident)
    binding._persist_consumer_secret(
        ctx,
        package=package,
        manifest=manifest,
        need=need,
        kind=kind,
        values=values,
    )
    upsert_env(env_path, INTERFACES[kind].bind(values, bind_env))
    if not _probe(ctx, kind, provider, provider_manifest, values, extensions, port=port):
        return _restore_after_probe_failure(
            name,
            kind,
            old,
            saved,
            runner=runner,
            root=root,
            image=image,
            script=script,
            package_dir=package_dir,
            consumer=package,
            admin=admin,
            secrets=secrets,
            host=host,
            port=port,
            database=ident,
            extensions=extensions,
        )

    services = _services_receiving(manifest, need)
    restarted = _recreate(ctx, manifest, services)
    if isinstance(restarted, Result):
        return restarted
    return Result(
        code=0,
        command=_COMMAND,
        summary=f"Rotated the {kind} secret of {name}.",
        target=name,
        warnings=list(ctx.extra_warnings),
        result={"rotated": {"kind": kind, "provider": provider}, "restarted": services},
        changes=1,
    )


def _need(manifest: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    needs = manifest.get("needs") if isinstance(manifest.get("needs"), list) else []
    for item in needs:
        if isinstance(item, dict) and item.get("kind") == kind:
            return item
    return None


def _register_saved(
    secrets: SecretRegistry,
    kind: str,
    bind_env: Mapping[str, Any],
    env_path: Path,
    secret_path: Path,
) -> None:
    """Register secret values already on disk so a later message cannot print them."""
    secret_params = INTERFACES[kind].secret_params
    current = read_env(env_path)
    for param, env_name in bind_env.items():
        if param in secret_params and isinstance(env_name, str) and current.get(env_name):
            secrets.register(current[env_name])
    for value in read_env(secret_path).values():
        secrets.register(value)


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore_files(saved: dict[Path, bytes | None]) -> None:
    for path, data in saved.items():
        if data is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)


def _provision(
    runner: CommandRunner,
    *,
    secret: str,
    root: Path,
    kind: str,
    image: str,
    script: str,
    package_dir: Path,
    consumer: str,
    admin: Mapping[str, str],
    secrets: SecretRegistry,
    host: str,
    port: int,
    database: str,
    extensions: list[str],
) -> ProvisionResult:
    return run_provision(
        runner,
        root=root,
        kind=kind,
        image=image,
        script=script,
        package_dir=package_dir,
        consumer=consumer,
        admin=admin,
        secret=secret,
        secrets=secrets,
        endpoint=f"http://{host}:{port}" if kind == "s3" else None,
        host=host if kind == "postgres" else None,
        port=port if kind == "postgres" else None,
        database=database if kind == "postgres" else None,
        extensions=extensions if kind == "postgres" else (),
    )


def _provision_error(name: str, result: ProvisionResult) -> Result:
    error_id = result.error_id or "provision_failed"
    spec = ERRORS[error_id]
    message = result.message or spec.title
    return Result(
        code=spec.code,
        command=_COMMAND,
        summary=message,
        target=name,
        error={"id": error_id, "message": message, "hint": spec.action},
        retryable=spec.retryable,
    )


def _consumer_values(
    kind: str,
    result: ProvisionResult,
    *,
    host: str,
    port: int,
    ident: str,
) -> dict[str, str]:
    """Same fields ``_apply_catalog`` keeps after provision."""
    values = dict(result.values)
    if kind == "s3":
        values["path_style"] = (
            "true" if str(values.get("path_style")).lower() in {"true", "1"} else "false"
        )
        values["endpoint"] = f"http://{host}:{port}"
        return values
    values["host"] = host
    values["port"] = str(port)
    values["sslmode"] = "disable"
    values["database"] = ident
    values["user"] = ident
    return values


def _probe(
    ctx: Context,
    kind: str,
    provider: str,
    provider_manifest: Mapping[str, Any],
    values: Mapping[str, str],
    extensions: list[str],
    *,
    port: int,
) -> bool:
    container = str(provider_manifest["provides"]["container"])
    service_name = f"{provider}-{container}"
    ip = binding._container_ip(ctx, service_name)
    if kind == "s3":
        endpoint = str(values["endpoint"])
        report = binding.probe_s3(
            binding._s3_params(values),
            connect_endpoint=binding._swap_endpoint(endpoint, ip) if ip else None,
        )
    else:
        report = binding.probe_postgres(
            binding._pg_params(values),
            extensions=tuple(extensions),
            connect_host=ip,
            connect_port=port if ip else None,
            require_isolation=True,
        )
    return binding._map_probe(ctx, kind, report)


def _restore_after_probe_failure(
    name: str,
    kind: str,
    old: str,
    saved: dict[Path, bytes | None],
    *,
    runner: CommandRunner,
    root: Path,
    image: str,
    script: str,
    package_dir: Path,
    consumer: str,
    admin: Mapping[str, str],
    secrets: SecretRegistry,
    host: str,
    port: int,
    database: str,
    extensions: list[str],
) -> Result:
    # Put the previous secret back at the provider before restoring files.
    # If that call fails, the files still hold the new secret and apply can retry it.
    restored = _provision(
        runner,
        secret=old,
        root=root,
        kind=kind,
        image=image,
        script=script,
        package_dir=package_dir,
        consumer=consumer,
        admin=admin,
        secrets=secrets,
        host=host,
        port=port,
        database=database,
        extensions=extensions,
    )
    if restored.ok:
        _restore_files(saved)
        return _error(
            _COMMAND,
            name,
            "rotate_failed",
            "The new secret did not work. The previous secret was restored.",
        )
    return _error(
        _COMMAND,
        name,
        "rotate_failed",
        "The service env files contain the new secret, and the provider is in an unknown state.",
        hint=(f"Run `service apply {name}`. It reuses the saved secret and provisions again."),
    )


def _services_receiving(manifest: Mapping[str, Any], need: Mapping[str, Any]) -> list[str]:
    package = str(manifest["name"])
    relative = binding._env_relpath(package, manifest, need)
    marker = "./" + relative.split("/", 1)[1]
    placed = placement(manifest)
    return [
        f"{package}-{role}"
        for role in package_roles(manifest)
        if marker in placed.fragment_env_files(role)
    ]


def _recreate(
    ctx: Context,
    manifest: Mapping[str, Any],
    services: list[str],
) -> Result | None:
    if not services:
        return None
    layout = _layout(ctx.lock)
    project = str(layout.get("project") or "")
    prefix = compose_prefix(ctx.root, layout, project=project or None)
    up = run(
        ctx.runner,
        [*prefix, "up", "-d", "--force-recreate", *services],
        cwd=ctx.root,
    )
    if up.returncode != 0:
        text = ctx.secrets.redact((up.stderr or up.stdout or "").strip())
        error_id = "docker_unavailable" if daemon_down(up) else "service_unhealthy"
        target = str(manifest.get("name") or "")
        return _error(_COMMAND, target, error_id, text or ERRORS[error_id].title)
    narrowed = dict(manifest)
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    name = str(manifest["name"])
    narrowed["containers"] = {
        role: spec
        for role, spec in containers.items()
        if isinstance(spec, dict) and f"{name}-{role}" in services
    }
    if _wait(ctx, narrowed, project):
        return None
    failure = ctx.failure
    error_id = "service_unhealthy"
    message = ERRORS[error_id].title
    hint = None
    if failure is not None and failure.error_id:
        error_id = failure.error_id
        message = failure.message or message
        hint = failure.hint or None
    return _error(_COMMAND, name, error_id, message, hint=hint)
