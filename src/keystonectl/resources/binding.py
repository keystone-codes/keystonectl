"""Bind needs for s3/v1 and postgres/v1: external resource or one catalog provider."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import yaml
from packaging.version import Version

from keystonectl import __version__
from keystonectl.answers import (
    QUESTION_TEMPLATES,
    AnswerError,
    Question,
    is_secret_var,
    pkg_var,
    read_secret_file,
)
from keystonectl.envfiles import read_env, upsert_env
from keystonectl.errors import ERRORS
from keystonectl.fetch import (
    FetchError,
    describe_fetch_error,
    fetch_tree,
    replace_tree,
    sha256_file,
)
from keystonectl.lock import save_lock, utc_now
from keystonectl.output import SecretRegistry
from keystonectl.plan import Context, StepPlan, register_step
from keystonectl.resources import INTERFACES, RESOURCE_NETWORK, UPSTREAMS_NETWORK
from keystonectl.resources.network import ip_for_service
from keystonectl.resources.postgres_probe import PgParams, probe_postgres
from keystonectl.resources.provision import (
    consumer_ident,
    consumer_secret_path,
    existing_consumer_secret,
    run_provision,
)
from keystonectl.resources.s3_probe import S3Params, probe_s3
from keystonectl.sources import (
    Resolution,
    ResolvedPackage,
    package_cache,
    warning,
)

_S3_PROBE_ERRORS = {
    "auth_failed": "s3_access_denied",
    "bucket_not_found": "s3_bucket_missing",
    "wrong_region": "s3_wrong_region",
    "presign_put_failed": "s3_presign_failed",
    "presign_get_failed": "s3_presign_failed",
    "presign_get_mismatch": "s3_presign_failed",
}
_PG_PROBE_ERRORS = {
    "auth_failed": "postgres_access_denied",
    "database_not_found": "postgres_database_missing",
    "version_too_old": "postgres_version_too_old",
    "create_table_failed": "postgres_create_table_failed",
    "not_isolated": "postgres_not_isolated",
    "extension_missing": "postgres_extension_missing",
}
_SPECIFIC = {
    "s3": {
        "bucket": "S3_BUCKET",
        "access_key": "S3_ACCESS_KEY_FILE",
        "secret_key": "S3_SECRET_KEY_FILE",
    },
    "postgres": {
        "database": "POSTGRES_DATABASE",
        "user": "POSTGRES_USER",
        "password": "POSTGRES_PASSWORD_FILE",
        "url": "POSTGRES_URL_FILE",
    },
}
_INSECURE_SSL = frozenset({"disable", "allow", "prefer"})


@dataclass
class _Work:
    actions: list[dict[str, str]] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


def _fail(ctx: Context, error_id: str, message: str, *, hint: str | None = None) -> None:
    spec = ERRORS[error_id]
    ctx.failure = Resolution(
        error_id=error_id,
        message=message,
        hint=hint if hint is not None else spec.action,
        result={"retryable": spec.retryable},
    )


def _manifest_path(ctx: Context, package: ResolvedPackage | None = None) -> Path | None:
    package = package or ctx.resolved
    if package is None:
        return None
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    relative = (
        Path("keystone-service.yaml")
        if package.path == "."
        else Path(package.path) / "keystone-service.yaml"
    )
    path = dest / relative
    return path if path.is_file() else None


def _ensure_manifest(ctx: Context) -> dict[str, Any] | None:
    if isinstance(ctx.manifest, dict):
        return ctx.manifest
    path = _manifest_path(ctx)
    if path is None:
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        ctx.manifest = loaded
        return loaded
    return None


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _services(lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in lock.get("services") or [] if isinstance(item, dict)]


def _resources(lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in lock.get("resources") or [] if isinstance(item, dict)]


def _resource(lock: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    for item in _resources(lock):
        if item.get("kind") == kind:
            return item
    return None


def _service(lock: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in _services(lock):
        if item.get("name") == name:
            return item
    return None


def _consumer(resource: Mapping[str, Any], package: str) -> dict[str, Any] | None:
    for item in resource.get("consumers") or []:
        if isinstance(item, dict) and item.get("service") == package:
            return item
    return None


def _origin_label(resource: Mapping[str, Any]) -> str:
    provider = resource.get("provider")
    if isinstance(provider, str) and provider:
        return provider
    return "external"


def _question(var: str, text: str | None = None, **overrides: Any) -> Question:
    template = QUESTION_TEMPLATES[var]
    return Question(
        var,
        text if text is not None else template.text,
        overrides.get("kind", template.kind),
        choices=overrides.get("choices", template.choices),
        default=overrides.get("default", template.default),
        secret=overrides.get("secret", template.secret),
        help=overrides.get("help", template.help),
    )


def _choice_question(kind: str, package: str) -> Question:
    if kind == "s3":
        text = (
            f"Where should {package} keep files: in S3 storage you already have (external), "
            "or in a new MinIO on this server (catalog)?"
        )
        var = "KEYSTONE_SETUP_RESOURCE_S3"
    else:
        text = (
            f"Which PostgreSQL should {package} use: a server you already have (external), "
            "or a new PostgreSQL on this server (catalog)?"
        )
        var = "KEYSTONE_SETUP_RESOURCE_POSTGRES"
    return _question(var, text)


def _connect_title(kind: str, package: str) -> str:
    if kind == "s3":
        return f"Connect s3 storage for {package}"
    return f"Connect a PostgreSQL database for {package}"


def _provision_title(kind: str, package: str, provider: str, extensions: list[str]) -> str:
    if kind == "s3":
        return f"Create bucket {package} and user {package} in {provider}"
    ident = consumer_ident(package)
    listed = ", ".join(extensions) if extensions else "none"
    return f"Create database {ident} and user {ident} in {provider}, extensions: {listed}"


def _env_with_saved_secret(
    root: Path,
    package: str,
    manifest: Mapping[str, Any],
    need: Mapping[str, Any],
    kind: str,
) -> dict[str, str]:
    """Env the next apply should read, including a secret saved before a failed probe."""
    merged = read_env(consumer_secret_path(root, package, kind))
    merged.update(read_env(root / _env_relpath(package, manifest, need)))
    return merged


def _persist_consumer_secret(
    ctx: Context,
    *,
    package: str,
    manifest: Mapping[str, Any],
    need: Mapping[str, Any],
    kind: str,
    values: Mapping[str, str],
) -> None:
    """Write the generated consumer secret before the probe.

    provision.sh does not change a password it already set. The package env
    file is written only after the probe succeeds, so the secret has to live
    in its own mode 600 file or the next apply would generate a new one.
    """
    bind_env = need.get("bind_env") if isinstance(need.get("bind_env"), dict) else {}
    bound = INTERFACES[kind].bind(values, bind_env)
    secret_params = INTERFACES[kind].secret_params
    kept: dict[str, str] = {}
    for param, env_name in bind_env.items():
        if param not in secret_params or not isinstance(env_name, str):
            continue
        value = bound.get(env_name)
        if not value:
            continue
        kept[env_name] = value
        ctx.secrets.register(value)
    if not kept:
        return
    path = consumer_secret_path(ctx.root, package, kind)
    upsert_env(path, kept)
    path.parent.chmod(0o700)


def _env_relpath(package: str, manifest: Mapping[str, Any], need: Mapping[str, Any]) -> str:
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    roles = set(containers)
    requested = need.get("containers")
    if isinstance(requested, list) and requested and set(requested) < roles:
        return f"packages/{package}.{need['kind']}.env"
    return f"packages/{package}.env"


def _bind_keys(need: Mapping[str, Any]) -> list[str]:
    bind_env = need.get("bind_env") if isinstance(need.get("bind_env"), dict) else {}
    return [value for value in bind_env.values() if isinstance(value, str)]


def _env_complete(root: Path, relpath: str, keys: list[str]) -> bool:
    if not keys:
        return True
    found = read_env(root / relpath)
    return all(key in found and found[key] != "" for key in keys)


def _extensions(need: Mapping[str, Any]) -> list[str]:
    raw = need.get("extensions") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _load_secret(path: Path, secrets: SecretRegistry) -> str:
    read_secret_file(path, secrets)
    text = path.read_text(encoding="utf-8")
    return text.removesuffix("\n").removesuffix("\r")


def _lookup(
    ctx: Context,
    question: Question,
    *,
    package: str,
    specific: str | None,
) -> str | None:
    """Return an answer. Secret answers are the file contents, not the path."""
    names: list[str] = []
    if specific:
        names.append(pkg_var(package, specific))
    names.append(question.var)
    service_answers: dict[str, str] = {}
    current = _service(ctx.lock, package)
    if current and isinstance(current.get("answers"), dict):
        service_answers = {str(key): str(value) for key, value in current["answers"].items()}
    for name in names:
        raw: str | None = None
        if name in ctx.sets:
            if is_secret_var(name):
                raise AnswerError(
                    "secret_in_argument",
                    "Refusing to accept a secret on the command line.",
                )
            raw = ctx.sets[name]
        elif ctx.environ.get(name):
            raw = str(ctx.environ[name])
        elif name in service_answers and service_answers[name] != "":
            raw = service_answers[name]
        if raw is None:
            continue
        if is_secret_var(name) or question.secret:
            return _load_secret(Path(raw), ctx.secrets)
        return raw
    if question.default is not None:
        return question.default
    return None


def _explicit_answer(ctx: Context, var: str) -> str | None:
    """Answer from --set or the environment only (not lock answers)."""
    if var in ctx.sets:
        if is_secret_var(var):
            raise AnswerError(
                "secret_in_argument",
                "Refusing to accept a secret on the command line.",
            )
        return ctx.sets[var]
    raw = ctx.environ.get(var)
    if raw:
        return str(raw)
    return None


def _check_ignored_resource_answers(
    ctx: Context,
    work: _Work,
    *,
    kind: str,
    package: str,
    bound: Mapping[str, Any],
) -> None:
    choice = _choice_question(kind, package)
    answer = _explicit_answer(ctx, choice.var)
    origin = bound.get("origin")
    if answer is not None and answer != origin:
        if origin == "catalog":
            provider = _origin_label(bound)
            detail = f"{kind} on this installation is already provided by {provider} (catalog)."
        else:
            detail = f"{kind} on this installation is already external storage."
        work.warnings.append(
            warning(
                "resource_answer_ignored",
                f"{choice.var}={answer} is ignored: {detail}",
            )
        )
    if origin != "catalog":
        return
    provider = str(bound.get("provider") or "")
    if not provider:
        return
    service = _service(ctx.lock, provider)
    if service is None:
        return
    source = str(service.get("source") or "")
    expected = f"{source}/{provider}"
    var = f"KEYSTONE_SETUP_RESOURCE_{kind.upper()}_PACKAGE"
    package_answer = _explicit_answer(ctx, var)
    if package_answer is not None and package_answer != expected:
        work.warnings.append(
            warning(
                "resource_answer_ignored",
                f"{var}={package_answer} is ignored: {kind} on this installation is already "
                f"provided by {provider} (catalog).",
            )
        )


def _remember_question(work: _Work, question: Question, value: str | None) -> str | None:
    if value is None:
        work.questions.append(question)
        return None
    return value


def _providers(ctx: Context, kind: str, version: int) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    for source in ctx.sources:
        catalog = ctx.catalogs.get(source.name) or {}
        packages = catalog.get("packages") if isinstance(catalog, dict) else None
        if not isinstance(packages, dict):
            continue
        for name, entry in packages.items():
            if not isinstance(entry, dict) or entry.get("kind") != "resource":
                continue
            provides = entry.get("provides") if isinstance(entry.get("provides"), dict) else {}
            if provides.get("kind") == kind and provides.get("version") == version:
                found.append((source.name, str(name), entry))
    return found


def _choose_version(entry: Mapping[str, Any]) -> dict[str, Any]:
    versions = [item for item in entry.get("versions") or [] if isinstance(item, dict)]
    return max(versions, key=lambda item: Version(str(item["version"])))


def _entry_version(entry: Mapping[str, Any], version: str) -> dict[str, Any] | None:
    for item in entry.get("versions") or []:
        if isinstance(item, dict) and item.get("version") == version:
            return item
    return None


def _resolved_provider(
    source: str, name: str, entry: Mapping[str, Any], *, pinned_version: str | None = None
) -> ResolvedPackage:
    """Build the provider's ResolvedPackage: the pinned (already installed) version when
    there is one, the newest compatible catalog version otherwise (a first install).

    Binding a second (or later) consumer to a provider that is already installed must
    reuse exactly what is running, not silently record the catalog's newest version in
    the lock while the container itself is never redeployed.
    """
    chosen = None
    if pinned_version is not None:
        chosen = _entry_version(entry, pinned_version)
    if chosen is None:
        chosen = _choose_version(entry)
    provides = entry.get("provides") if isinstance(entry.get("provides"), dict) else None
    return ResolvedPackage(
        source_name=source,
        name=name,
        version=str(chosen["version"]),
        ref=str(chosen["ref"]),
        git_url=str(entry.get("source") or ""),
        path=str(entry.get("path") or "."),
        kind="resource",
        keystone_mcp=None,
        provides=provides,
    )


def _select_provider(
    ctx: Context,
    work: _Work,
    *,
    kind: str,
    version: int,
    package: str,
) -> tuple[str, str, dict[str, Any]] | None:
    found = _providers(ctx, kind, version)
    if not found:
        _fail(
            ctx,
            "no_provider",
            f"No {kind} provider is in the configured catalogs.",
        )
        return None
    if len(found) == 1:
        return found[0]
    var = f"KEYSTONE_SETUP_RESOURCE_{kind.upper()}_PACKAGE"
    choices = tuple(sorted(f"{source}/{name}" for source, name, _entry in found))
    question = _question(
        var,
        choices=choices,
        help=f"Example: {choices[0]}",
    )
    try:
        picked = _lookup(ctx, question, package=package, specific=None)
    except AnswerError as exc:
        _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
        return None
    if picked is None:
        work.questions.append(question)
        return None
    if picked not in choices:
        _fail(
            ctx,
            "usage",
            f"{var} must be one of: {', '.join(choices)}.",
        )
        return None
    source_name, pkg_name = picked.split("/", 1)
    for source, name, entry in found:
        if source == source_name and name == pkg_name:
            return source, name, entry
    return None


def _bound_params(resource: Mapping[str, Any], name: str) -> str | None:
    params = resource.get("params") if isinstance(resource.get("params"), dict) else {}
    if name not in params:
        return None
    value = params[name]
    if name == "path_style":
        return "true" if value is True else "false" if value is False else str(value)
    return str(value)


def _external_values(
    ctx: Context,
    work: _Work,
    *,
    kind: str,
    package: str,
    need: Mapping[str, Any],
    bound: Mapping[str, Any] | None,
    ask_consumer: bool,
) -> dict[str, str] | None:
    values: dict[str, str] = {}
    url_question = QUESTION_TEMPLATES["KEYSTONE_SETUP_POSTGRES_URL_FILE"]
    url_value: str | None = None
    if kind == "postgres":
        try:
            url_value = _lookup(
                ctx,
                url_question,
                package=package,
                specific=_SPECIFIC["postgres"]["url"],
            )
        except AnswerError as exc:
            _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
            return None
        if url_value is not None:
            parsed = urlparse(url_value)
            if "sslmode" not in parse_qs(parsed.query):
                _fail(
                    ctx,
                    "postgres_url_invalid",
                    "KEYSTONE_SETUP_POSTGRES_URL_FILE must include sslmode "
                    "(for example ?sslmode=require).",
                )
                return None
            ctx.secrets.register(url_value)
            values["url"] = url_value
    if kind == "postgres" and url_value is not None:
        from keystonectl.resources.postgres_probe import ParamsError, params_from_url

        try:
            parsed_params = params_from_url(url_value)
        except ParamsError as exc:
            _fail(ctx, "postgres_url_invalid", str(exc))
            return None
        ctx.secrets.register(parsed_params.password)
        host = parsed_params.host
        port = str(parsed_params.port)
        sslmode = parsed_params.sslmode
        if bound is not None:
            recorded = (
                _bound_params(bound, "host"),
                _bound_params(bound, "port"),
                _bound_params(bound, "sslmode"),
            )
            if any(item is None for item in recorded) or recorded != (host, port, sslmode):
                _fail(
                    ctx,
                    "postgres_address_mismatch",
                    "PostgreSQL is already bound. Host, port, and sslmode from "
                    "KEYSTONE_SETUP_POSTGRES_URL_FILE do not match the recorded "
                    "address, so that address was not changed.",
                )
                return None
            host, port, sslmode = recorded
        values.update(
            {
                "host": host,
                "port": port,
                "database": parsed_params.database,
                "user": parsed_params.user,
                "password": parsed_params.password,
                "sslmode": sslmode,
            }
        )
        values["url"] = interface_url(values)
        if sslmode in _INSECURE_SSL:
            work.warnings.append(
                warning(
                    "postgres_plain_connection",
                    f"PostgreSQL sslmode is {sslmode}; the connection is not verified.",
                )
            )
        return values

    resource_questions = {
        "s3": {
            "endpoint": _question("KEYSTONE_SETUP_S3_ENDPOINT"),
            "region": _question("KEYSTONE_SETUP_S3_REGION"),
            "path_style": _question("KEYSTONE_SETUP_S3_PATH_STYLE"),
        },
        "postgres": {
            "host": _question("KEYSTONE_SETUP_POSTGRES_HOST"),
            "port": _question("KEYSTONE_SETUP_POSTGRES_PORT"),
            "sslmode": _question("KEYSTONE_SETUP_POSTGRES_SSLMODE"),
        },
    }
    consumer_questions = {
        "s3": {
            "bucket": _question("KEYSTONE_SETUP_S3_BUCKET"),
            "access_key": _question("KEYSTONE_SETUP_S3_ACCESS_KEY_FILE"),
            "secret_key": _question("KEYSTONE_SETUP_S3_SECRET_KEY_FILE"),
        },
        "postgres": {
            "database": _question("KEYSTONE_SETUP_POSTGRES_DATABASE"),
            "user": _question("KEYSTONE_SETUP_POSTGRES_USER"),
            "password": _question("KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE"),
        },
    }
    try:
        for name, question in resource_questions[kind].items():
            if bound is not None and _bound_params(bound, name) is not None:
                values[name] = _bound_params(bound, name) or ""
                continue
            values[name] = (
                _remember_question(
                    work,
                    question,
                    _lookup(ctx, question, package=package, specific=None),
                )
                or ""
            )
        if ask_consumer:
            for name, question in consumer_questions[kind].items():
                specific = _SPECIFIC[kind].get(name)
                looked = _lookup(ctx, question, package=package, specific=specific)
                values[name] = _remember_question(work, question, looked) or ""
    except AnswerError as exc:
        _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
        return None
    if work.questions or ctx.failure is not None:
        return None
    if kind == "s3" and values.get("endpoint", "").startswith("http://"):
        work.warnings.append(
            warning(
                "s3_plain_http",
                "The S3 endpoint uses http://. Traffic to this storage is not encrypted.",
            )
        )
    if kind == "postgres" and values.get("sslmode") in _INSECURE_SSL:
        work.warnings.append(
            warning(
                "postgres_plain_connection",
                f"PostgreSQL sslmode is {values['sslmode']}; the connection is not verified.",
            )
        )
    if kind == "postgres":
        values["url"] = interface_url(values)
    return values


def interface_url(values: Mapping[str, str]) -> str:
    return INTERFACES["postgres"].bind(values, {"url": "URL"})["URL"]


def _version_problem(
    package: str,
    kind: str,
    need_version: object,
    *,
    provider_name: str | None = None,
    provider_version: object | None = None,
    locked_version: object | None = None,
) -> str | None:
    if need_version != 1:
        return (
            f"{package} needs {kind} version {need_version}; this installer binds {kind}/v1 only."
        )
    if provider_version is not None and provider_version != need_version:
        return (
            f"{package} needs {kind}/{need_version}, but {provider_name} provides "
            f"{kind}/{provider_version}."
        )
    if locked_version is not None and locked_version != need_version:
        return (
            f"{package} needs {kind}/{need_version}, but the bound resource is "
            f"{kind}/{locked_version}."
        )
    return None


def _decide_need(ctx: Context, manifest: Mapping[str, Any], need: Mapping[str, Any]) -> _Work:
    work = _Work()
    package = str(manifest.get("name") or "")
    kind = str(need.get("kind") or "")
    if kind not in INTERFACES:
        _fail(ctx, "manifest_invalid", f"{package} needs unknown resource {kind}.")
        return work
    problem = _version_problem(package, kind, need.get("version"))
    if problem:
        _fail(ctx, "manifest_invalid", problem)
        return work
    bound = _resource(ctx.lock, kind)
    if bound is not None:
        problem = _version_problem(
            package,
            kind,
            need.get("version"),
            locked_version=bound.get("interface_version"),
        )
        if problem:
            _fail(ctx, "manifest_invalid", problem)
            return work
        _check_ignored_resource_answers(ctx, work, kind=kind, package=package, bound=bound)
    extensions = _extensions(need)
    relpath = _env_relpath(package, manifest, need)
    keys = _bind_keys(need)
    consumer = _consumer(bound, package) if bound else None
    env_ok = consumer is not None and _env_complete(ctx.root, relpath, keys)

    if bound is not None and consumer is not None and env_ok:
        work.actions.append(
            {
                "id": f"resources.{kind}",
                "title": _connect_title(kind, package),
                "status": "unchanged",
            }
        )
        return work

    if bound is not None and bound.get("origin") == "catalog":
        provider = str(bound.get("provider") or "")
        work.actions.append(
            {
                "id": "consumer.provision",
                "title": _provision_title(kind, package, provider, extensions),
                "status": "planned",
            }
        )
        work.actions.append(
            {
                "id": "consumer.probe",
                "title": f"Check {kind} access for {package}",
                "status": "planned",
            }
        )
        return work

    if bound is not None and bound.get("origin") == "external":
        _external_values(
            ctx,
            work,
            kind=kind,
            package=package,
            need=need,
            bound=bound,
            ask_consumer=True,
        )
        if ctx.failure is not None:
            return work
        if work.questions:
            work.actions.insert(
                0,
                {
                    "id": f"resources.{kind}",
                    "title": _connect_title(kind, package),
                    "status": "planned",
                },
            )
        else:
            work.actions.append(
                {
                    "id": "external.probe",
                    "title": (
                        f"Check the external S3 storage for {package}"
                        if kind == "s3"
                        else f"Check the external PostgreSQL database for {package}"
                    ),
                    "status": "planned",
                }
            )
        return work

    choice = _choice_question(kind, package)
    try:
        origin = _lookup(ctx, choice, package=package, specific=None)
    except AnswerError as exc:
        _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
        return work
    if origin is None:
        work.questions.append(choice)
        work.actions.append(
            {
                "id": f"resources.{kind}",
                "title": _connect_title(kind, package),
                "status": "planned",
            }
        )
        return work
    if origin not in {"external", "catalog"}:
        _fail(
            ctx,
            "usage",
            f"{choice.var} must be external or catalog.",
        )
        return work
    if origin == "external":
        _external_values(
            ctx,
            work,
            kind=kind,
            package=package,
            need=need,
            bound=None,
            ask_consumer=True,
        )
        if ctx.failure is not None:
            return work
        if work.questions:
            work.actions.insert(
                0,
                {
                    "id": f"resources.{kind}",
                    "title": _connect_title(kind, package),
                    "status": "planned",
                },
            )
        else:
            work.actions.append(
                {
                    "id": "external.probe",
                    "title": (
                        f"Check the external S3 storage for {package}"
                        if kind == "s3"
                        else f"Check the external PostgreSQL database for {package}"
                    ),
                    "status": "planned",
                }
            )
        return work

    selected = _select_provider(ctx, work, kind=kind, version=1, package=package)
    if ctx.failure is not None or selected is None:
        if work.questions and not any(item["id"] == f"resources.{kind}" for item in work.actions):
            work.actions.append(
                {
                    "id": f"resources.{kind}",
                    "title": _connect_title(kind, package),
                    "status": "planned",
                }
            )
        return work
    source, name, entry = selected
    provides = entry.get("provides") if isinstance(entry.get("provides"), dict) else {}
    problem = _version_problem(
        package,
        kind,
        need.get("version"),
        provider_name=name,
        provider_version=provides.get("version"),
    )
    if problem:
        _fail(ctx, "manifest_invalid", problem)
        return work
    chosen = _choose_version(entry)
    installed = _service(ctx.lock, name)
    deploy_status = "unchanged" if installed and installed.get("role") == "resource" else "planned"
    if deploy_status == "planned":
        work.actions.append(
            {
                "id": "provider.deploy",
                "title": (f"Install {name} {chosen['version']} ({source}) as the {kind} provider"),
                "status": "planned",
            }
        )
        work.actions.append(
            {
                "id": "provider.wait_healthy",
                "title": f"Wait until {name} is healthy",
                "status": "planned",
            }
        )
    work.actions.append(
        {
            "id": "consumer.provision",
            "title": _provision_title(kind, package, name, extensions),
            "status": "planned",
        }
    )
    work.actions.append(
        {
            "id": "consumer.probe",
            "title": f"Check {kind} access for {package}",
            "status": "planned",
        }
    )
    return work


def _provider_needs_deploy(
    ctx: Context, manifest: Mapping[str, Any], name: str, installed: Mapping[str, Any] | None
) -> bool:
    """True when ``provider.deploy``/``_apply_direct`` has to actually run.

    A provider is unchanged only when it is installed, bound, at the manifest's
    version, *and* the files apply would write (the fragment and
    docker-compose.packages.yml, which carries the override-include flag) are
    already on disk as apply would render them. Comparing only "installed and
    bound" let ``service upgrade``/``apply`` report success for a version bump
    or a newly added ``packages/<name>.override.yml`` without redeploying,
    regenerating the fragment, or including the override.
    """
    if installed is None:
        return True
    version = str(manifest.get("version") or "")
    if installed.get("version") != version:
        return True
    from keystonectl.generate.compose import render_fragment
    from keystonectl.generate.install import packages_file_text

    path = ctx.root / "packages" / f"{name}.yml"
    fragment = render_fragment(manifest, {})
    if not path.is_file() or path.read_text(encoding="utf-8") != fragment:
        return True
    packages_path = ctx.root / "docker-compose.packages.yml"
    if not packages_path.is_file():
        return True
    return packages_path.read_text(encoding="utf-8") != packages_file_text(ctx, name)


def _decide_provider(ctx: Context, manifest: Mapping[str, Any]) -> _Work:
    work = _Work()
    name = str(manifest.get("name") or "")
    provides = manifest.get("provides") if isinstance(manifest.get("provides"), dict) else {}
    kind = str(provides.get("kind") or "")
    if kind not in INTERFACES:
        _fail(ctx, "manifest_invalid", f"{name} does not provide s3/v1 or postgres/v1.")
        return work
    problem = _version_problem(name, kind, provides.get("version"), provider_name=name)
    if problem:
        _fail(ctx, "manifest_invalid", problem)
        return work
    bound = _resource(ctx.lock, kind)
    if bound is not None and bound.get("provider") != name:
        _fail(
            ctx,
            "resource_already_bound",
            f"{kind} is already provided by {_origin_label(bound)}; one provider per kind",
        )
        return work
    installed = _service(ctx.lock, name)
    source = ctx.resolved.source_name if ctx.resolved is not None else ""
    version = str(manifest.get("version") or "")
    status = (
        "planned"
        if bound is None or _provider_needs_deploy(ctx, manifest, name, installed)
        else "unchanged"
    )
    work.actions.append(
        {
            "id": "provider.deploy",
            "title": f"Install {name} {version} ({source}) as the {kind} provider",
            "status": status,
        }
    )
    work.actions.append(
        {
            "id": "provider.wait_healthy",
            "title": f"Wait until {name} is healthy",
            "status": status,
        }
    )
    return work


def _decide(ctx: Context) -> _Work:
    work = _Work()
    manifest = _ensure_manifest(ctx)
    if manifest is None or ctx.resolved is None:
        return work
    if manifest.get("kind") == "resource":
        return _decide_provider(ctx, manifest)
    needs = manifest.get("needs") if isinstance(manifest.get("needs"), list) else []
    for need in needs:
        if not isinstance(need, dict):
            continue
        piece = _decide_need(ctx, manifest, need)
        work.actions.extend(piece.actions)
        work.questions.extend(piece.questions)
        work.warnings.extend(piece.warnings)
        if ctx.failure is not None:
            break
    return work


def _container_address(manifest: Mapping[str, Any]) -> tuple[str, int]:
    provides = manifest["provides"]
    role = str(provides["container"])
    port = int(manifest["containers"][role]["port"])
    host = f"{manifest['name']}-{role}"
    return host, port


def _stamp(ctx: Context) -> str:
    return utc_now(ctx.clock())


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lock_containers(manifest: Mapping[str, Any]) -> dict[str, Any]:
    name = str(manifest["name"])
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    rendered: dict[str, Any] = {}
    for role, spec in containers.items():
        if not isinstance(spec, dict):
            continue
        rendered[str(role)] = {
            "service": f"{name}-{role}",
            "image": str(spec.get("image") or ""),
            "init": bool(spec.get("init", False)),
        }
    return rendered


def _fragment_digest(root: Path | None, manifest: Mapping[str, Any], name: str) -> str:
    from keystonectl.generate.compose import fragment_digest, render_fragment

    if root is not None:
        path = root / "packages" / f"{name}.yml"
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    return fragment_digest(render_fragment(manifest, {}))


def _ensure_service(
    lock: dict[str, Any],
    *,
    package: ResolvedPackage,
    manifest: Mapping[str, Any],
    role: str,
    now: str,
    env_files: list[str],
    resources: list[str],
    generated: list[str],
    root: Path | None = None,
) -> dict[str, Any]:
    current = _service(lock, package.name)
    if current is None:
        compat = manifest.get("compat") if isinstance(manifest.get("compat"), dict) else {}
        entry: dict[str, Any] = {
            "name": package.name,
            "role": role,
            "managed": "generated",
            "source": package.source_name,
            "package": package.name,
            "version": package.version,
            "ref": package.ref,
            "commit": package.commit,
            "manifest_sha256": package.manifest_sha256,
            "containers": _lock_containers(manifest),
            "fragment": f"packages/{package.name}.yml",
            "fragment_sha256": _fragment_digest(root, manifest, package.name),
            "registry_name": None,
            "namespace": None,
            "url": None,
            "tools": [],
            "filters": None,
            "env": {"keystone": [], "files": list(env_files), "generated": list(generated)},
            "answers": {},
            "resources": list(resources),
            "public": {"state": "none"},
            "retired_tools": [],
            "previous": None,
            "installed_at": now,
            "updated_at": now,
        }
        if role == "service" and isinstance(compat.get("keystone_mcp"), str):
            entry["compat_keystone_mcp"] = compat["keystone_mcp"]
        lock.setdefault("services", []).append(entry)
        return entry
    compat = manifest.get("compat") if isinstance(manifest.get("compat"), dict) else {}
    current["source"] = package.source_name
    current["version"] = package.version
    current["ref"] = package.ref
    current["commit"] = package.commit
    current["manifest_sha256"] = package.manifest_sha256
    current["containers"] = _lock_containers(manifest)
    if role == "service" and isinstance(compat.get("keystone_mcp"), str):
        current["compat_keystone_mcp"] = compat["keystone_mcp"]
    env = current.setdefault("env", {"keystone": [], "files": [], "generated": []})
    if isinstance(env, dict):
        files = env.setdefault("files", [])
        for item in env_files:
            if item not in files:
                files.append(item)
        generated_names = env.setdefault("generated", [])
        for item in generated:
            if item not in generated_names:
                generated_names.append(item)
    listed = current.setdefault("resources", [])
    for kind in resources:
        if kind not in listed:
            listed.append(kind)
    current["updated_at"] = now
    return current


def _upsert_resource(
    lock: dict[str, Any],
    *,
    kind: str,
    origin: str,
    provider: str | None,
    params: dict[str, Any],
    consumer: dict[str, Any] | None,
) -> None:
    resources = lock.setdefault("resources", [])
    current = _resource(lock, kind)
    if current is None:
        current = {
            "kind": kind,
            "interface_version": 1,
            "origin": origin,
            "provider": provider,
            "params": params,
            "consumers": [],
        }
        resources.append(current)
    else:
        current["params"] = params
        current["origin"] = origin
        current["provider"] = provider
    if consumer is None:
        return
    consumers = current.setdefault("consumers", [])
    for index, item in enumerate(consumers):
        if isinstance(item, dict) and item.get("service") == consumer["service"]:
            consumers[index] = consumer
            return
    consumers.append(consumer)


def _s3_params(values: Mapping[str, str]) -> S3Params:
    return S3Params(
        endpoint=values["endpoint"],
        region=values.get("region") or "us-east-1",
        bucket=values["bucket"],
        access_key=values["access_key"],
        secret_key=values["secret_key"],
        path_style=str(values.get("path_style", "true")).lower() == "true",
    )


def _pg_params(values: Mapping[str, str]) -> PgParams:
    return PgParams(
        host=values["host"],
        port=int(values["port"]),
        database=values["database"],
        user=values["user"],
        password=values["password"],
        sslmode=values.get("sslmode") or "require",
    )


def _public_probe(report: Any, secrets: SecretRegistry) -> dict[str, Any]:
    data = report.to_json_dict()
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        error["message"] = secrets.redact(error["message"])
    for key in ("access_key", "secret_key", "password", "url"):
        data.pop(key, None)
    return data


def _map_probe(ctx: Context, kind: str, report: Any) -> bool:
    ctx.probe_reports[kind] = _public_probe(report, ctx.secrets)
    code = report.exit_code
    if code == 0:
        return True
    if code == 4 and kind == "s3":
        ctx.extra_warnings.append(
            warning(
                "s3_lifecycle_unsupported",
                "storage works, but old uploads will not be deleted automatically",
            )
        )
        return True
    if code == 2:
        missing = list(getattr(report, "missing", []) or [])
        for name in missing:
            var = _var_for_param(kind, name)
            if var:
                ctx.halt_questions.append(_question(var))
        if not ctx.halt_questions:
            _fail(ctx, "manifest_invalid", "The resource probe reported missing parameters.")
        return False
    if code == 3:
        error_id = "s3_unreachable" if kind == "s3" else "postgres_unreachable"
        message = ERRORS[error_id].title
        if report.error is not None and report.error.message:
            message = ctx.secrets.redact(report.error.message)
        _fail(ctx, error_id, message)
        return False
    probe_code = report.error.code if report.error is not None else ""
    table = _S3_PROBE_ERRORS if kind == "s3" else _PG_PROBE_ERRORS
    error_id = table.get(
        probe_code, "s3_access_denied" if kind == "s3" else "postgres_access_denied"
    )
    message = ERRORS[error_id].title
    if probe_code == "extension_missing" and report.error is not None:
        names = report.error.missing_extensions or []
        if names:
            message = f"Missing PostgreSQL extensions: {', '.join(names)}."
    elif report.error is not None and report.error.message:
        message = ctx.secrets.redact(report.error.message)
    _fail(ctx, error_id, message)
    return False


def _var_for_param(kind: str, name: str) -> str | None:
    if kind == "s3":
        return {
            "endpoint": "KEYSTONE_SETUP_S3_ENDPOINT",
            "region": "KEYSTONE_SETUP_S3_REGION",
            "bucket": "KEYSTONE_SETUP_S3_BUCKET",
            "access_key": "KEYSTONE_SETUP_S3_ACCESS_KEY_FILE",
            "secret_key": "KEYSTONE_SETUP_S3_SECRET_KEY_FILE",
            "path_style": "KEYSTONE_SETUP_S3_PATH_STYLE",
        }.get(name)
    return {
        "host": "KEYSTONE_SETUP_POSTGRES_HOST",
        "port": "KEYSTONE_SETUP_POSTGRES_PORT",
        "database": "KEYSTONE_SETUP_POSTGRES_DATABASE",
        "user": "KEYSTONE_SETUP_POSTGRES_USER",
        "password": "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE",
        "sslmode": "KEYSTONE_SETUP_POSTGRES_SSLMODE",
    }.get(name)


def _swap_endpoint(endpoint: str, ip: str) -> str:
    parsed = urlparse(endpoint)
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse((parsed.scheme or "http", f"{ip}{port}", "", "", "", ""))


def _container_ip(ctx: Context, name: str) -> str | None:
    deployer = ctx.deployer
    if deployer is not None:
        return str(deployer.container_ip(ctx, name, RESOURCE_NETWORK))
    for network in (RESOURCE_NETWORK, UPSTREAMS_NETWORK):
        result = ctx.runner.run(
            ["docker", "network", "inspect", network, "--format", "{{json .Containers}}"]
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        ip = ip_for_service(payload, name)
        if ip:
            return ip
    return None


def _host_without_dot(host: str) -> bool:
    return bool(host) and "." not in host and not host.startswith("[")


def _probe_external(ctx: Context, kind: str, values: dict[str, str], extensions: list[str]) -> bool:
    ip: str | None = None
    if kind == "s3":
        host = urlparse(values["endpoint"]).hostname or ""
        if _host_without_dot(host):
            ip = _find_alias_ip(ctx, host)
        report = probe_s3(
            _s3_params(values),
            connect_endpoint=_swap_endpoint(values["endpoint"], ip) if ip else None,
        )
    else:
        host = values["host"]
        if _host_without_dot(host):
            ip = _find_alias_ip(ctx, host)
        report = probe_postgres(
            _pg_params(values),
            extensions=tuple(extensions),
            connect_host=ip,
            connect_port=int(values["port"]) if ip else None,
            require_isolation=False,
        )
    return _map_probe(ctx, kind, report)


def _find_alias_ip(ctx: Context, name: str) -> str | None:
    for network in (RESOURCE_NETWORK, UPSTREAMS_NETWORK):
        result = ctx.runner.run(
            ["docker", "network", "inspect", network, "--format", "{{json .Containers}}"]
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        ip = ip_for_service(payload, name)
        if ip:
            return ip
    return None


def _generated_names(manifest: Mapping[str, Any]) -> list[str]:
    provides = manifest.get("provides") if isinstance(manifest.get("provides"), dict) else {}
    role = provides.get("container")
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    target = containers.get(role) if isinstance(role, str) else None
    if not isinstance(target, dict):
        return []
    names: list[str] = []
    for item in target.get("secrets") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _admin_values(ctx: Context, manifest: Mapping[str, Any]) -> dict[str, str]:
    provision = manifest["provision"]
    admin = provision["admin_env"]
    role = manifest["provides"]["container"]
    container = manifest["containers"][role]
    secret_names = {
        item["name"]
        for item in container.get("secrets") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    defaults: dict[str, str] = {}
    for item in container.get("env") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and "default" in item:
            defaults[item["name"]] = str(item["default"])
    path = ctx.root / "packages" / f"{manifest['name']}.env"
    current = read_env(path)
    values = dict(current)
    for slot in ("user", "password"):
        name = str(admin[slot])
        if values.get(name):
            if name in secret_names:
                ctx.secrets.register(values[name])
            continue
        if name in secret_names:
            from secrets import token_hex

            values[name] = token_hex(32 if slot == "password" else 16)
            ctx.secrets.register(values[name])
        elif name in defaults:
            values[name] = defaults[name]
    to_write = {
        key: value
        for key, value in values.items()
        if key in secret_names or key in defaults or key in {admin["user"], admin["password"]}
    }
    if to_write:
        upsert_env(path, to_write)
    return {
        str(admin["user"]): values[str(admin["user"])],
        str(admin["password"]): values[str(admin["password"])],
    }


def _fetch_package(ctx: Context, package: ResolvedPackage) -> dict[str, Any] | None:
    if not package.git_url or not package.ref:
        _fail(ctx, "package_not_found", f"Package {package.name} has no source.")
        return None
    try:
        tree, commit = fetch_tree(ctx.runner, package.git_url, package.ref, ctx.root)
    except FetchError as exc:
        error_id = getattr(exc, "error_id", "source_unreachable")
        _fail(ctx, error_id, describe_fetch_error(f"Package {package.name}", exc))
        return None
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    replace_tree(tree, dest)
    manifest_path = _manifest_path(ctx, package)
    if manifest_path is None:
        _fail(ctx, "manifest_invalid", f"{package.name} has no keystone-service.yaml.")
        return None
    package.commit = commit
    package.manifest_sha256 = sha256_file(manifest_path)
    loaded = _load_yaml(manifest_path)
    if loaded is None:
        _fail(ctx, "manifest_invalid", f"{package.name} has no keystone-service.yaml.")
        return None
    from keystonectl.generate.install import _reject_manifest

    if _reject_manifest(ctx, loaded):
        return None
    return loaded


def _commit_consumer(
    ctx: Context,
    *,
    consumer_package: ResolvedPackage,
    consumer_manifest: Mapping[str, Any],
    need: Mapping[str, Any],
    kind: str,
    origin: str,
    provider: str | None,
    provider_package: ResolvedPackage | None,
    provider_manifest: Mapping[str, Any] | None,
    values: Mapping[str, str],
    extensions: list[str],
) -> None:
    now = _stamp(ctx)
    relpath = _env_relpath(str(consumer_manifest["name"]), consumer_manifest, need)
    bind_env = need.get("bind_env") if isinstance(need.get("bind_env"), dict) else {}
    bound = INTERFACES[kind].bind(values, bind_env)
    for value in bound.values():
        ctx.secrets.register(value) if _looks_secret(kind, bind_env, bound, value) else None
    upsert_env(ctx.root / relpath, bound)
    if provider_package is not None and provider_manifest is not None:
        _ensure_service(
            ctx.lock,
            package=provider_package,
            manifest=provider_manifest,
            role="resource",
            now=now,
            env_files=[f"packages/{provider_package.name}.env"],
            resources=[],
            generated=_generated_names(provider_manifest),
            root=ctx.root,
        )
    _ensure_service(
        ctx.lock,
        package=consumer_package,
        manifest=consumer_manifest,
        role="service",
        now=now,
        env_files=[relpath],
        resources=[kind],
        generated=[],
        root=ctx.root,
    )
    if kind == "s3":
        params = {
            "endpoint": values["endpoint"],
            "region": values.get("region") or "us-east-1",
            "path_style": str(values.get("path_style", "true")).lower() == "true",
        }
        consumer = {
            "service": consumer_package.name,
            "bucket": values["bucket"],
            "provisioned_at": now,
        }
    else:
        params = {
            "host": values["host"],
            "port": int(values["port"]),
            "sslmode": values.get("sslmode") or "disable",
        }
        consumer = {
            "service": consumer_package.name,
            "database": values["database"],
            "user": values["user"],
            "extensions": list(extensions),
            "provisioned_at": now,
        }
    _upsert_resource(
        ctx.lock,
        kind=kind,
        origin=origin,
        provider=provider,
        params=params,
        consumer=consumer,
    )


def _looks_secret(
    kind: str,
    bind_env: Mapping[str, str],
    bound: Mapping[str, str],
    value: str,
) -> bool:
    secret_params = INTERFACES[kind].secret_params
    for param, env_name in bind_env.items():
        if param in secret_params and bound.get(env_name) == value:
            return True
    return False


def _apply_external(
    ctx: Context,
    manifest: Mapping[str, Any],
    need: Mapping[str, Any],
) -> None:
    package = str(manifest["name"])
    kind = str(need["kind"])
    work = _Work()
    bound = _resource(ctx.lock, kind)
    values = _external_values(
        ctx,
        work,
        kind=kind,
        package=package,
        need=need,
        bound=bound,
        ask_consumer=True,
    )
    if ctx.failure is not None:
        return
    if work.questions or values is None:
        ctx.halt_questions.extend(work.questions)
        return
    if not _probe_external(ctx, kind, values, _extensions(need)):
        return
    assert ctx.resolved is not None
    _commit_consumer(
        ctx,
        consumer_package=ctx.resolved,
        consumer_manifest=manifest,
        need=need,
        kind=kind,
        origin="external",
        provider=None,
        provider_package=None,
        provider_manifest=None,
        values=values,
        extensions=_extensions(need),
    )


def _apply_catalog(
    ctx: Context,
    manifest: Mapping[str, Any],
    need: Mapping[str, Any],
) -> None:
    package = str(manifest["name"])
    kind = str(need["kind"])
    bound = _resource(ctx.lock, kind)
    if bound is not None and isinstance(bound.get("provider"), str):
        provider_name = str(bound["provider"])
        provider_service = _service(ctx.lock, provider_name)
        source_name = str((provider_service or {}).get("source") or "")
        entry = _catalog_entry(ctx, source_name, provider_name)
        if entry is None:
            _fail(ctx, "no_provider", f"Provider {provider_name} is not in the catalogs anymore.")
            return
        selected = (source_name, provider_name, entry)
    else:
        work = _Work()
        selected_maybe = _select_provider(ctx, work, kind=kind, version=1, package=package)
        if ctx.failure is not None or selected_maybe is None:
            ctx.halt_questions.extend(work.questions)
            return
        selected = selected_maybe
    source, name, entry = selected
    installed = _service(ctx.lock, name)
    pinned_version = (
        str(installed["version"])
        if installed is not None and isinstance(installed.get("version"), str)
        else None
    )
    provider_package = _resolved_provider(source, name, entry, pinned_version=pinned_version)
    if installed is None:
        if ctx.deployer is None:
            _fail(
                ctx,
                "not_implemented",
                ERRORS["not_implemented"].title,
            )
            return
        provider_manifest = _fetch_package(ctx, provider_package)
        if provider_manifest is None:
            return
        ctx.deployer.deploy(ctx, provider_package, role="resource")
        if ctx.failure is not None:
            return
        ctx.deployer.wait_healthy(ctx, name, timeout=120)
        if ctx.failure is not None:
            return
    else:
        provider_manifest = _fetch_package(ctx, provider_package)
        if provider_manifest is None:
            return
        if ctx.deployer is not None:
            ctx.deployer.wait_healthy(ctx, name, timeout=120)
    if provider_manifest is None:
        return
    host, port = _container_address(provider_manifest)
    admin = _admin_values(ctx, provider_manifest)
    extensions = _extensions(need)
    ident = consumer_ident(package) if kind == "postgres" else package
    secret = existing_consumer_secret(
        _env_with_saved_secret(ctx.root, package, manifest, need, kind),
        need.get("bind_env") or {},
    )
    package_dir = package_cache(ctx.root, source, name, provider_package.version)
    if provider_package.path != ".":
        package_dir = package_dir / provider_package.path
    provision = provider_manifest["provision"]
    result = run_provision(
        ctx.runner,
        root=ctx.root,
        kind=kind,
        image=str(provision["image"]),
        script=str(provision["script"]),
        package_dir=package_dir,
        consumer=package,
        admin=admin,
        secret=secret,
        secrets=ctx.secrets,
        endpoint=f"http://{host}:{port}" if kind == "s3" else None,
        host=host if kind == "postgres" else None,
        port=port if kind == "postgres" else None,
        database=ident if kind == "postgres" else None,
        extensions=extensions if kind == "postgres" else (),
    )
    if not result.ok:
        retryable = result.retryable
        ctx.failure = Resolution(
            error_id=result.error_id or "provision_failed",
            message=result.message,
            hint=ERRORS[result.error_id or "provision_failed"].action,
            result={"retryable": retryable},
        )
        return
    values = dict(result.values)
    # The probe dials the container by IP. These fields are what the service
    # stores, so stdout cannot aim the consumer secret at another server.
    if kind == "s3":
        values["path_style"] = (
            "true" if str(values.get("path_style")).lower() in {"true", "1"} else "false"
        )
        values["endpoint"] = f"http://{host}:{port}"
    else:
        values["host"] = host
        values["port"] = str(port)
        values["sslmode"] = "disable"
        values["database"] = ident
        values["user"] = ident
    _persist_consumer_secret(
        ctx,
        package=package,
        manifest=manifest,
        need=need,
        kind=kind,
        values=values,
    )
    service_name = f"{name}-{provider_manifest['provides']['container']}"
    ip = (
        _container_ip(ctx, service_name)
        if ctx.deployer is not None
        else _find_alias_ip(ctx, service_name)
    )
    if kind == "s3":
        report = probe_s3(
            _s3_params(values),
            connect_endpoint=_swap_endpoint(values["endpoint"], ip) if ip else None,
        )
    else:
        report = probe_postgres(
            _pg_params(values),
            extensions=tuple(extensions),
            connect_host=ip,
            connect_port=port if ip else None,
            require_isolation=True,
        )
    if not _map_probe(ctx, kind, report):
        return
    assert ctx.resolved is not None
    _commit_consumer(
        ctx,
        consumer_package=ctx.resolved,
        consumer_manifest=manifest,
        need=need,
        kind=kind,
        origin="catalog",
        provider=name,
        provider_package=provider_package,
        provider_manifest=provider_manifest,
        values=values,
        extensions=extensions,
    )


def _catalog_entry(ctx: Context, source: str, name: str) -> dict[str, Any] | None:
    catalog = ctx.catalogs.get(source) or {}
    packages = catalog.get("packages") if isinstance(catalog, dict) else None
    if not isinstance(packages, dict):
        for catalog in ctx.catalogs.values():
            packages = catalog.get("packages") if isinstance(catalog, dict) else None
            if isinstance(packages, dict) and isinstance(packages.get(name), dict):
                return packages[name]
        return None
    entry = packages.get(name)
    return entry if isinstance(entry, dict) else None


def _apply_direct(ctx: Context, manifest: Mapping[str, Any]) -> None:
    if ctx.deployer is None:
        _fail(ctx, "not_implemented", ERRORS["not_implemented"].title)
        return
    assert ctx.resolved is not None
    name = str(manifest["name"])
    bound = _resource(ctx.lock, str(manifest["provides"]["kind"]))
    installed = _service(ctx.lock, name)
    if bound is None or _provider_needs_deploy(ctx, manifest, name, installed):
        ctx.deployer.deploy(ctx, ctx.resolved, role="resource")
        if ctx.failure is not None:
            return
        ctx.deployer.wait_healthy(ctx, name, timeout=120)
        if ctx.failure is not None:
            return
    kind = str(manifest["provides"]["kind"])
    host, port = _container_address(manifest)
    if kind == "s3":
        params: dict[str, Any] = {
            "endpoint": f"http://{host}:{port}",
            "region": "us-east-1",
            "path_style": True,
        }
    else:
        params = {"host": host, "port": port, "sslmode": "disable"}
    now = _stamp(ctx)
    _admin_values(ctx, manifest)
    entry = _ensure_service(
        ctx.lock,
        package=ctx.resolved,
        manifest=manifest,
        role="resource",
        now=now,
        env_files=[f"packages/{name}.env"],
        resources=[],
        generated=_generated_names(manifest),
        root=ctx.root,
    )
    # `_ensure_service` only sets `fragment_sha256` on a first install (the
    # regular-service path patches it again afterwards in `_save_service`); a
    # provider upgrade never did, so a redeployed provider kept its old
    # version's hash and the next `service upgrade`/`service plan` would see
    # a false hand-edit (`fragment_modified`) against the file deploy() just
    # wrote.
    entry["fragment_sha256"] = _fragment_digest(ctx.root, manifest, name)
    entry["fragment"] = f"packages/{name}.yml"
    _upsert_resource(
        ctx.lock,
        kind=kind,
        origin="catalog",
        provider=name,
        params=params,
        consumer=None,
    )


def _save(ctx: Context) -> None:
    ctx.lock["updated_by"] = f"keystonectl {__version__}"
    save_lock(ctx.root / "services.lock.yaml", ctx.lock, now=ctx.clock())


class ResourcesStep:
    """Pipeline step inserted after requires."""

    id = "resources"
    is_service_deploy = False

    def plan(self, ctx: Context) -> StepPlan:
        ctx.failure = None if _failure_from_elsewhere(ctx) else ctx.failure
        work = _decide(ctx)
        return StepPlan(actions=work.actions, questions=work.questions, warnings=work.warnings)

    def apply(self, ctx: Context) -> None:
        manifest = _ensure_manifest(ctx)
        if manifest is None or ctx.resolved is None or ctx.failure is not None:
            return
        before = copy.deepcopy(ctx.lock)
        if manifest.get("kind") == "resource":
            work = _decide_provider(ctx, manifest)
            if ctx.failure is not None:
                return
            from keystonectl.generate.install import fragment_write_blocked

            blocked = fragment_write_blocked(ctx, str(manifest.get("name") or ""))
            unchanged = work.actions and all(item["status"] == "unchanged" for item in work.actions)
            if blocked != "modified" and not unchanged:
                _apply_direct(ctx, manifest)
        else:
            needs = manifest.get("needs") if isinstance(manifest.get("needs"), list) else []
            for need in needs:
                if not isinstance(need, dict) or ctx.failure is not None or ctx.halt_questions:
                    break
                kind = str(need.get("kind") or "")
                bound = _resource(ctx.lock, kind)
                consumer = _consumer(bound, str(manifest["name"])) if bound else None
                relpath = _env_relpath(str(manifest["name"]), manifest, need)
                if (
                    bound is not None
                    and consumer is not None
                    and _env_complete(ctx.root, relpath, _bind_keys(need))
                ):
                    continue
                if bound is not None and bound.get("origin") == "catalog":
                    _apply_catalog(ctx, manifest, need)
                elif bound is not None and bound.get("origin") == "external":
                    _apply_external(ctx, manifest, need)
                else:
                    choice = _choice_question(kind, str(manifest["name"]))
                    try:
                        origin = _lookup(ctx, choice, package=str(manifest["name"]), specific=None)
                    except AnswerError as exc:
                        _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
                        break
                    if origin == "catalog":
                        _apply_catalog(ctx, manifest, need)
                    elif origin == "external":
                        _apply_external(ctx, manifest, need)
        if ctx.failure is not None or ctx.halt_questions:
            ctx.lock.clear()
            ctx.lock.update(before)
            return
        if ctx.lock != before:
            _save(ctx)


def _failure_from_elsewhere(ctx: Context) -> bool:
    return False


def unbind_consumer(lock: dict[str, Any], pkg: str) -> list[str]:
    """Drop a consumer from resources[]. Buckets and databases stay."""
    messages: list[str] = []
    for resource in _resources(lock):
        consumers = resource.get("consumers")
        if not isinstance(consumers, list):
            continue
        kept = []
        for consumer in consumers:
            if not isinstance(consumer, dict) or consumer.get("service") != pkg:
                kept.append(consumer)
                continue
            provider = _origin_label(resource)
            if resource.get("kind") == "s3":
                messages.append(f"bucket {consumer.get('bucket')} in s3 ({provider}) was kept")
            else:
                messages.append(
                    f"database {consumer.get('database')} in postgres ({provider}) was kept"
                )
        resource["consumers"] = kept
    for service in _services(lock):
        if service.get("name") == pkg and isinstance(service.get("resources"), list):
            service["resources"] = []
    return messages


def provider_removal_error(lock: Mapping[str, Any], provider: str) -> Resolution | None:
    """Refuse removal while any consumer still uses this provider."""
    used: list[str] = []
    for resource in _resources(lock):
        if resource.get("provider") != provider:
            continue
        for consumer in resource.get("consumers") or []:
            if isinstance(consumer, dict) and consumer.get("service"):
                used.append(str(consumer["service"]))
    if not used:
        return None
    return Resolution(
        error_id="resource_in_use",
        message=f"Cannot remove {provider}: still used by {', '.join(used)}.",
        hint=ERRORS["resource_in_use"].action,
    )


def idle_catalog_providers(lock: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Catalog providers still installed but with no consumers left."""
    installed = {
        str(item.get("name"))
        for item in _services(lock)
        if isinstance(item, dict) and item.get("name")
    }
    found: list[tuple[str, str]] = []
    for resource in _resources(lock):
        if resource.get("origin") != "catalog":
            continue
        consumers = [item for item in resource.get("consumers") or [] if isinstance(item, dict)]
        if consumers:
            continue
        provider = str(resource.get("provider") or "")
        if provider not in installed:
            continue
        found.append((provider, str(resource.get("kind") or "")))
    return found


def provider_unused_message(provider: str, kind: str, volume_names: list[str]) -> str:
    kept_label = "kept buckets" if kind == "s3" else "kept databases"
    volumes = ", ".join(volume_names) if volume_names else "the provider volumes"
    return (
        f"{provider} has no consumers left. Its data, including the {kept_label}, "
        f"is in {volumes}. Remove it with `keystonectl service remove {provider}` "
        f"when you no longer need it."
    )


def provider_data_kept_message(provider: str, volume_names: list[str]) -> str:
    listed = ", ".join(volume_names)
    return (
        f"Volumes {listed} still hold the data of every former consumer. "
        f"Installing {provider} again reuses them. "
        f"Delete them with `docker volume rm {' '.join(volume_names)}` only after a backup."
    )


def register_resources() -> None:
    from keystonectl.plan import _EXTRAS

    if any(getattr(step, "id", None) == "resources" for step, _after in _EXTRAS):
        return
    register_step(ResourcesStep(), after="requires")


register_resources()
