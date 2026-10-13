"""service plan/apply pipeline. Later cards insert steps with ``register_step``."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from keystonectl.answers import Question
from keystonectl.catalog import CatalogError, load_sources
from keystonectl.compat import (
    catalog_field_mismatches,
    detect_keystone_mcp_version,
    missing_requires,
    ranges_differ,
)
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.fetch import FetchError, fetch_tree, replace_tree, sha256_file
from keystonectl.keystone_mcp_rules import BUILTIN_TOOL_NAMES_VERSION
from keystonectl.lock import load_lock
from keystonectl.manifest import manifest_errors
from keystonectl.output import Result, SecretRegistry, missing_summary
from keystonectl.runner import CommandRunner
from keystonectl.sources import (
    Resolution,
    ResolvedPackage,
    failure_result,
    installed_service,
    load_cached_catalogs,
    package_cache,
    refresh_all,
    resolve_package,
    warning,
)

BUILTIN_STEP_IDS = {"resolve", "fetch", "validate", "compat", "requires", "lock.commit"}
_EXTRAS: list[tuple[Any, str | None]] = []


@dataclass
class StepPlan:
    actions: list[dict[str, str]] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Context:
    root: Path
    runner: CommandRunner
    secrets: SecretRegistry
    sets: dict[str, str]
    environ: Mapping[str, str]
    interactive: bool
    lock: dict[str, Any]
    clock: Callable[[], datetime]
    target: str = ""
    sources: list[Any] = field(default_factory=list)
    extra_warnings: list[dict[str, str]] = field(default_factory=list)
    catalogs: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: ResolvedPackage | None = None
    resolution: Resolution | None = None
    keystone_version: str | None = None
    refresh_needed: bool = False
    failure: Resolution | None = None
    manifest: dict[str, Any] | None = None
    manifest_sha256: str | None = None
    problems: list[dict[str, str]] = field(default_factory=list)
    deployer: Any = None
    probe_reports: dict[str, Any] = field(default_factory=dict)
    halt_questions: list[Any] = field(default_factory=list)
    output_result: dict[str, Any] = field(default_factory=dict)
    rerun: bool = False
    defer_public: bool = False
    certbot_staging: bool = False
    http: Any = None
    nginx_root: Path | None = None
    le_live: Path | None = None
    custom_dir: Path | None = None
    cron_file: Path | None = None
    handshake: Any = None
    assume_installed: frozenset[str] = field(default_factory=frozenset)
    action_log: list[dict[str, str]] = field(default_factory=list)
    skip_actions: set[str] = field(default_factory=set)
    chosen_port: int | None = None


def register_step(step: Any, after: str | None = None) -> None:
    _EXTRAS.append((step, after))


def reset_steps() -> None:
    _EXTRAS.clear()


def _caches_ready(root: Path, sources: list[Any]) -> bool:
    return all(
        (root / ".keystonectl" / "catalogs" / source.name / "catalog.yaml").is_file()
        for source in sources
    )


def _questions_from(resolution: Resolution | None) -> list[Question]:
    if resolution is None:
        return []
    found: list[Question] = []
    for item in resolution.missing:
        found.append(
            Question(
                str(item["var"]),
                str(item["question"]),
                str(item["kind"]),
                choices=tuple(item["choices"]) if item.get("choices") else None,
                default=item.get("default"),
                secret=bool(item.get("secret")),
            )
        )
    return found


def _download_title(package: ResolvedPackage) -> str:
    return f"Download {package.name} {package.version} (tag {package.ref})"


def _same_install(lock: Mapping[str, Any], package: ResolvedPackage, commit: str | None) -> bool:
    installed = installed_service(lock, package.name)
    if not installed or commit is None:
        return False
    return installed.get("version") == package.version and installed.get("commit") == commit


class ResolveStep:
    id = "resolve"

    def plan(self, ctx: Context) -> StepPlan:
        detection = detect_keystone_mcp_version(
            ctx.root,
            lock=ctx.lock,
            environ=ctx.environ,
            answers=ctx.sets,
        )
        ctx.keystone_version = detection.version
        questions = [detection.question] if detection.question is not None else []
        try:
            ctx.sources = load_sources(ctx.root / "sources.yaml")
        except CatalogError as exc:
            ctx.failure = Resolution(
                error_id="source_invalid",
                message="; ".join(exc.messages),
                hint=ERRORS["source_invalid"].action,
            )
            return StepPlan(questions=questions)
        ctx.catalogs = load_cached_catalogs(ctx.root, ctx.sources)
        if not _caches_ready(ctx.root, ctx.sources):
            ctx.refresh_needed = True
            return StepPlan(
                actions=[
                    {
                        "id": "resolve",
                        "title": "Update catalogs and resolve the package",
                        "status": "planned",
                    }
                ],
                questions=questions,
            )
        return self._resolve(ctx, questions)

    def _resolve(self, ctx: Context, questions: list[Question]) -> StepPlan:
        resolution = resolve_package(
            ctx.target,
            sources=ctx.sources,
            catalogs=ctx.catalogs,
            lock=ctx.lock,
            keystone_version=ctx.keystone_version,
        )
        ctx.resolution = resolution
        ctx.resolved = resolution.package
        if resolution.error_id and not resolution.missing:
            ctx.failure = resolution
        questions = questions + _questions_from(resolution)
        title = "Resolve package"
        if resolution.package is not None:
            title = f"Resolve {resolution.package.name} {resolution.package.version}"
        return StepPlan(
            actions=[{"id": "resolve", "title": title, "status": "unchanged"}],
            questions=questions,
            warnings=list(resolution.warnings),
        )

    def apply(self, ctx: Context) -> None:
        return None


class FetchStep:
    id = "fetch"

    def plan(self, ctx: Context) -> StepPlan:
        package = ctx.resolved
        if package is None:
            return StepPlan()
        installed = installed_service(ctx.lock, package.name)
        status = "planned"
        if (
            installed
            and installed.get("version") == package.version
            and installed.get("commit")
            and package.commit
            and installed.get("commit") == package.commit
        ):
            status = "unchanged"
        return StepPlan(
            actions=[{"id": "fetch", "title": _download_title(package), "status": status}]
        )

    def apply(self, ctx: Context) -> None:
        return None


class ValidateStep:
    id = "validate"

    def plan(self, ctx: Context) -> StepPlan:
        return StepPlan(
            actions=[
                {"id": "validate", "title": "Validate the package manifest", "status": "planned"}
            ]
        )

    def apply(self, ctx: Context) -> None:
        return None


class CompatStep:
    id = "compat"

    def plan(self, ctx: Context) -> StepPlan:
        kind = ctx.resolved.kind if ctx.resolved is not None else None
        if isinstance(ctx.manifest, dict) and ctx.manifest.get("kind"):
            kind = str(ctx.manifest.get("kind"))
        if kind == "resource":
            return StepPlan()
        return StepPlan(
            actions=[
                {"id": "compat", "title": "Check keystone-mcp compatibility", "status": "planned"}
            ]
        )

    def apply(self, ctx: Context) -> None:
        return None


class RequiresStep:
    id = "requires"

    def plan(self, ctx: Context) -> StepPlan:
        return StepPlan(
            actions=[{"id": "requires", "title": "Check required packages", "status": "planned"}]
        )

    def apply(self, ctx: Context) -> None:
        return None


class LockCommitStep:
    id = "lock.commit"

    def plan(self, ctx: Context) -> StepPlan:
        return StepPlan(
            actions=[
                {
                    "id": "lock.commit",
                    "title": "Record the package in services.lock.yaml",
                    "status": "planned",
                }
            ]
        )

    def apply(self, ctx: Context) -> None:
        return None


def _builtin() -> list[Any]:
    return [
        ResolveStep(),
        FetchStep(),
        ValidateStep(),
        CompatStep(),
        RequiresStep(),
        LockCommitStep(),
    ]


def build_steps() -> list[Any]:
    from keystonectl.compose_gen import ensure_registered

    ensure_registered()
    steps = _builtin()
    for step, after in _EXTRAS:
        if after is None:
            steps.append(step)
            continue
        indexes = [index for index, item in enumerate(steps) if item.id == after]
        if not indexes:
            steps.append(step)
            continue
        steps.insert(indexes[0] + 1, step)
    return steps


def _has_deploy(steps: list[Any]) -> bool:
    """True when a later card registered the service deploy step.

    The resources step binds storage. It does not deploy the service itself.
    """
    return any(
        step.id not in BUILTIN_STEP_IDS and getattr(step, "is_service_deploy", True)
        for step in steps
    )


def _bundle(
    plans: list[StepPlan],
) -> tuple[list[Question], list[dict[str, str]], list[dict[str, str]]]:
    questions = _dedupe_questions([question for plan in plans for question in plan.questions])
    warnings = [item for plan in plans for item in plan.warnings]
    actions = [item for plan in plans for item in plan.actions]
    return questions, warnings, actions


def _manifest_ready(ctx: Context) -> bool:
    package = ctx.resolved
    if package is None:
        return False
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    relative = (
        Path("keystone-service.yaml")
        if package.path == "."
        else Path(package.path) / "keystone-service.yaml"
    )
    return (dest / relative).is_file()


def _replan_resources(ctx: Context, steps: list[Any], plans: list[StepPlan]) -> None:
    for index, step in enumerate(steps):
        if getattr(step, "id", None) in {"resources", "deploy", "public"}:
            plans[index] = step.plan(ctx)


def _resource_actions(steps: list[Any], plans: list[StepPlan]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for step, plan in zip(steps, plans, strict=True):
        if getattr(step, "id", None) == "resources":
            found.extend(plan.actions)
    return found


def _failure_result(ctx: Context, command: str, warnings: list[dict[str, str]]) -> Result:
    assert ctx.failure is not None
    result = failure_result(ctx.failure, command=command, target=ctx.target)
    result.warnings = warnings
    if ctx.failure.error_id == "provision_failed":
        result.retryable = bool(ctx.failure.result.get("retryable"))
    if ctx.failure.error_id == "docker_unavailable":
        result.retryable = True
    if ctx.probe_reports:
        result.result = {**result.result, "probes": ctx.probe_reports}
    if ctx.action_log:
        result.actions = list(ctx.action_log)
    if ctx.output_result:
        result.result = {**ctx.output_result, **result.result}
    return result


def _dedupe_questions(questions: list[Question]) -> list[Question]:
    seen: set[str] = set()
    unique: list[Question] = []
    for question in questions:
        if question.var in seen:
            continue
        seen.add(question.var)
        unique.append(question)
    return unique


def _refresh(ctx: Context) -> str | None:
    catalogs, warnings, error_id = refresh_all(ctx.root, ctx.sources, ctx.runner)
    ctx.catalogs = catalogs
    ctx.extra_warnings.extend(warnings)
    return error_id


def _fetch_package(ctx: Context) -> str | None:
    package = ctx.resolved
    if package is None:
        return "package_not_found"
    if not package.git_url or not package.ref:
        return "package_not_found"
    try:
        tree, commit = fetch_tree(ctx.runner, package.git_url, package.ref)
    except FetchError as exc:
        return exc.error_id
    installed = installed_service(ctx.lock, package.name)
    if (
        installed
        and installed.get("version") == package.version
        and isinstance(installed.get("commit"), str)
        and installed["commit"] != commit
    ):
        import shutil

        shutil.rmtree(tree, ignore_errors=True)
        ctx.failure = Resolution(
            error_id="tag_moved",
            message=(
                f"Tag {package.ref} for {package.name} {package.version} moved "
                f"from {installed['commit']} to {commit}."
            ),
            hint=ERRORS["tag_moved"].action,
        )
        return "tag_moved"
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    replace_tree(tree, dest)
    manifest_path = dest / package.path / "keystone-service.yaml"
    if package.path == ".":
        manifest_path = dest / "keystone-service.yaml"
    package.commit = commit
    if manifest_path.is_file():
        package.manifest_sha256 = sha256_file(manifest_path)
        ctx.manifest_sha256 = package.manifest_sha256
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        ctx.manifest = loaded if isinstance(loaded, dict) else None
    return None


def _validate_manifest(ctx: Context) -> Resolution | None:
    if ctx.manifest is None:
        return Resolution(
            error_id="manifest_invalid",
            message="The package has no keystone-service.yaml.",
            hint=ERRORS["manifest_invalid"].action,
        )
    lines = manifest_errors(ctx.manifest)
    problems = [
        {"path": line.split(": ", 1)[0], "message": line, "severity": "error"} for line in lines
    ]
    entry = {
        "name": ctx.resolved.name if ctx.resolved else None,
        "kind": ctx.resolved.kind if ctx.resolved else None,
        "version": ctx.resolved.version if ctx.resolved else None,
        "provides": ctx.resolved.provides if ctx.resolved else None,
        "keystone_mcp": ctx.resolved.keystone_mcp if ctx.resolved else None,
    }
    for field_name in catalog_field_mismatches(ctx.manifest, entry):
        problems.append(
            {
                "path": field_name,
                "message": f"{field_name} does not match the catalog",
                "severity": "error",
            }
        )
    ctx.problems = problems
    if problems:
        return Resolution(
            error_id="manifest_invalid",
            message=problems[0]["message"],
            hint=ERRORS["manifest_invalid"].action,
            result={"problems": problems},
        )
    manifest_range = (
        (ctx.manifest.get("compat") or {}) if isinstance(ctx.manifest.get("compat"), dict) else {}
    ).get("keystone_mcp")
    if ctx.resolved and ranges_differ(
        manifest_range if isinstance(manifest_range, str) else None,
        ctx.resolved.keystone_mcp,
    ):
        ctx.extra_warnings.append(
            warning(
                "compat_mismatch",
                "The manifest compatibility range differs from the catalog. The manifest range is used.",
            )
        )
    if ctx.keystone_version:
        try:
            stale = Version(ctx.keystone_version) > Version(BUILTIN_TOOL_NAMES_VERSION)
        except InvalidVersion:
            stale = False
        if stale:
            ctx.extra_warnings.append(
                warning("builtin_list_may_be_stale", WARNINGS["builtin_list_may_be_stale"].action)
            )
    missing = [
        name
        for name in missing_requires(ctx.manifest, ctx.lock)
        if name not in ctx.assume_installed
    ]
    if missing:
        return Resolution(
            error_id="requires_missing",
            message=f"Missing required packages: {', '.join(missing)}.",
            hint="Install them first or use `bundle install`.",
            result={"requires": missing},
        )
    return None


def run_service(
    root: Path,
    verb: str,
    target: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: dict[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock: Callable[[], datetime] | None = None,
    deployer: Any = None,
    defer_public: bool = False,
    certbot_staging: bool = False,
    http: Any = None,
    nginx_root: Path | None = None,
    le_live: Path | None = None,
    custom_dir: Path | None = None,
    cron_file: Path | None = None,
    handshake: Any = None,
    assume_installed: Collection[str] | None = None,
) -> Result:
    command = f"service {verb}"
    ctx = Context(
        root=root,
        runner=runner,
        secrets=secrets,
        sets=sets,
        environ=environ,
        interactive=interactive,
        lock=load_lock(root / "services.lock.yaml"),
        clock=clock or (lambda: datetime.now(UTC)),
        target=target,
        deployer=deployer,
        defer_public=defer_public,
        certbot_staging=certbot_staging,
        http=http,
        nginx_root=nginx_root,
        le_live=le_live,
        custom_dir=custom_dir,
        cron_file=cron_file,
        handshake=handshake,
        assume_installed=frozenset(assume_installed or ()),
    )
    steps = build_steps()
    plans: list[StepPlan] = []
    for step in steps:
        if step.id == "resolve":
            plans.append(step.plan(ctx))
            continue
        plans.append(step.plan(ctx))
    questions = _dedupe_questions([question for plan in plans for question in plan.questions])
    warnings = [item for plan in plans for item in plan.warnings]
    actions = [item for plan in plans for item in plan.actions]
    if questions:
        return Result(
            code=2,
            command=command,
            summary=missing_summary(len(questions)),
            target=target,
            actions=actions,
            missing=[question.as_missing() for question in questions],
            warnings=warnings,
            changes=0,
        )
    if ctx.refresh_needed:
        error_id = _refresh(ctx)
        warnings.extend(ctx.extra_warnings)
        ctx.extra_warnings.clear()
        if error_id:
            spec = ERRORS[error_id]
            return Result(
                code=spec.code,
                command=command,
                summary=spec.title,
                target=target,
                warnings=warnings,
                error={"id": spec.id, "message": spec.title, "hint": spec.action},
                retryable=spec.retryable,
            )
        resolve = ResolveStep()
        planned = resolve._resolve(ctx, [])
        questions = _dedupe_questions(planned.questions)
        warnings.extend(planned.warnings)
        if questions:
            return Result(
                code=2,
                command=command,
                summary=missing_summary(len(questions)),
                target=target,
                missing=[question.as_missing() for question in questions],
                warnings=warnings,
                changes=0,
            )
        if ctx.failure is not None:
            return failure_result(ctx.failure, command=command, target=target)
    elif ctx.failure is not None:
        return failure_result(ctx.failure, command=command, target=target)

    if ctx.resolved is None:
        spec = ERRORS["package_not_found"]
        return Result(
            code=spec.code,
            command=command,
            summary=spec.title,
            target=target,
            error={"id": spec.id, "message": spec.title, "hint": spec.action},
        )

    installed = installed_service(ctx.lock, ctx.resolved.name)
    already = bool(
        installed and installed.get("version") == ctx.resolved.version and installed.get("commit")
    )
    if verb == "apply" and not _has_deploy(steps) and not already and not _manifest_ready(ctx):
        return Result(
            code=1,
            command=command,
            summary=ERRORS["not_implemented"].title,
            target=target,
            warnings=warnings,
            error={
                "id": "not_implemented",
                "message": ERRORS["not_implemented"].title,
                "hint": ERRORS["not_implemented"].action,
            },
            changes=0,
        )

    noted: list[dict[str, str]] = []
    if verb in {"plan", "apply"}:
        if not ctx.refresh_needed:
            error_id = _refresh(ctx)
            warnings.extend(ctx.extra_warnings)
            ctx.extra_warnings.clear()
            if error_id:
                spec = ERRORS[error_id]
                return Result(
                    code=spec.code,
                    command=command,
                    summary=spec.title,
                    target=target,
                    warnings=warnings,
                    error={"id": spec.id, "message": spec.title, "hint": spec.action},
                    retryable=spec.retryable,
                )
            ResolveStep()._resolve(ctx, [])
            if ctx.failure is not None:
                return failure_result(ctx.failure, command=command, target=target)
        fetch_error = _fetch_package(ctx)
        if fetch_error:
            if ctx.failure is not None:
                return failure_result(ctx.failure, command=command, target=target)
            spec = ERRORS[fetch_error]
            return Result(
                code=spec.code,
                command=command,
                summary=spec.title,
                target=target,
                warnings=warnings,
                error={"id": spec.id, "message": spec.title, "hint": spec.action},
                retryable=spec.retryable,
            )
        problem = _validate_manifest(ctx)
        noted = list(ctx.extra_warnings)
        ctx.extra_warnings.clear()
        if problem is not None:
            return failure_result(problem, command=command, target=target)

    _replan_resources(ctx, steps, plans)
    questions, bundled, actions = _bundle(plans)
    warnings = bundled + noted + list(ctx.extra_warnings)
    ctx.extra_warnings.clear()
    if questions:
        return Result(
            code=2,
            command=command,
            summary=missing_summary(len(questions)),
            target=target,
            actions=actions,
            missing=[question.as_missing() for question in questions],
            warnings=warnings,
            changes=0,
        )
    if ctx.failure is not None:
        return _failure_result(ctx, command, warnings)

    resource_actions = _resource_actions(steps, plans)
    needs_deployer = any(
        action["id"] == "provider.deploy" and action["status"] == "planned"
        for action in resource_actions
    )
    resource_work = any(action["status"] == "planned" for action in resource_actions)

    if verb == "plan":
        from keystonectl.generate.install import service_install_would_rerun

        would_rerun = service_install_would_rerun(ctx)
        if would_rerun:
            for action in actions:
                if action.get("status") == "planned":
                    action["status"] = "unchanged"
        else:
            for action in actions:
                if action["id"] == "fetch" and ctx.resolved and ctx.resolved.commit:
                    action["title"] = _download_title(ctx.resolved)
                    action["status"] = "planned"
        return Result(
            code=0,
            command=command,
            summary=f"Plan for {ctx.resolved.name} {ctx.resolved.version}.",
            target=target,
            actions=actions,
            warnings=warnings,
            changes=0 if would_rerun else None,
            result={"commit": ctx.resolved.commit, "version": ctx.resolved.version},
        )

    if verb == "apply" and needs_deployer and ctx.deployer is None:
        return Result(
            code=1,
            command=command,
            summary=ERRORS["not_implemented"].title,
            target=target,
            actions=actions,
            warnings=warnings,
            error={
                "id": "not_implemented",
                "message": ERRORS["not_implemented"].title,
                "hint": ERRORS["not_implemented"].action,
            },
            changes=0,
        )

    if verb == "apply" and not resource_work:
        from keystonectl.generate.install import ComposeDeployer

        real_deploy = isinstance(ctx.deployer, ComposeDeployer)
        bound_again = bool(
            resource_actions and all(action["status"] == "unchanged" for action in resource_actions)
        )
        if not real_deploy:
            if bound_again or _same_install(ctx.lock, ctx.resolved, ctx.resolved.commit):
                for action in actions:
                    action["status"] = "unchanged"
                return Result(
                    code=0,
                    command=command,
                    summary="Already installed.",
                    target=target,
                    actions=actions,
                    warnings=warnings,
                    changes=0,
                    result={"commit": ctx.resolved.commit, "version": ctx.resolved.version},
                )
            return Result(
                code=1,
                command=command,
                summary=ERRORS["not_implemented"].title,
                target=target,
                error={
                    "id": "not_implemented",
                    "message": ERRORS["not_implemented"].title,
                    "hint": ERRORS["not_implemented"].action,
                },
                changes=0,
            )

    for step in steps:
        step.apply(ctx)
        if ctx.halt_questions:
            return Result(
                code=2,
                command=command,
                summary=missing_summary(len(ctx.halt_questions)),
                target=target,
                actions=actions,
                missing=[question.as_missing() for question in ctx.halt_questions],
                warnings=warnings + list(ctx.extra_warnings),
                changes=0,
            )
        if ctx.failure is not None:
            return _failure_result(ctx, command, warnings + list(ctx.extra_warnings))
    if ctx.rerun:
        for action in actions:
            action["status"] = "unchanged"
    else:
        for action in actions:
            if action["id"] in ctx.skip_actions:
                action["status"] = "skipped"
            elif action["status"] == "planned":
                action["status"] = "done"
    if ctx.action_log:
        actions = [*actions, *ctx.action_log]
    result_body: dict[str, Any] = {"commit": ctx.resolved.commit, "version": ctx.resolved.version}
    if ctx.probe_reports:
        result_body["probes"] = ctx.probe_reports
    result_body.update(ctx.output_result)
    changes = 0 if ctx.rerun or all(action["status"] == "unchanged" for action in actions) else None
    return Result(
        code=0,
        command=command,
        summary=f"Applied {ctx.resolved.name} {ctx.resolved.version}.",
        target=target,
        actions=actions,
        warnings=warnings + list(ctx.extra_warnings),
        changes=changes,
        result=result_body,
    )
