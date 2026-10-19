"""``containers.<role>.package_dir``: package files in a container, read-only (ADR 0010)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from keystonectl.answers import pkg_var
from keystonectl.check import run_check
from keystonectl.cli import main
from keystonectl.generate.compose import render_fragment
from keystonectl.lifecycle import (
    eject_service,
    remove_service,
    rollback_service,
    upgrade_service,
)
from keystonectl.lock import load_lock, save_lock
from keystonectl.manifest import manifest_errors
from keystonectl.output import SecretRegistry
from keystonectl.package_dirs import (
    MAX_BYTES,
    MAX_FILES,
    PackageDirError,
    ensure_copies,
    plan,
    subtree_digest,
)
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.sources import package_cache
from tests.support import SHA
from tests.support.deploylab import CLOCK, DockerBox, apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"
PROVIDER = {"provider/provider.yaml": "name: demo\n", "provider/lib/requirements.txt": "x==1\n"}
_MKFIFO = getattr(os, "mkfifo", None)


def _document(*, version: str = "1.0.0", source: str = "provider") -> dict:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["version"] = version
    mcp = document["containers"]["mcp"]
    mcp.pop("env", None)
    mcp["package_dir"] = {"from": source, "path": "/srv/provider"}
    return document


def _text(**kwargs: str) -> str:
    return yaml.safe_dump(_document(**kwargs), sort_keys=False)


def _copies(root: Path) -> list[str]:
    base = root / ".keystonectl" / "package-dirs" / "file-mcp"
    if not base.is_dir():
        return []
    return sorted(child.name for child in base.iterdir())


def _apply(root: Path, *, files: dict[str, str] | None = None, box: DockerBox | None = None):
    return apply_package(
        root,
        "file-mcp",
        {"file-mcp": _text()},
        box=box,
        package_files={"file-mcp": PROVIDER if files is None else files},
    )


# Schema and manifest rules


def _errors(container_extra: dict, *, kind_resource: bool = False) -> list[str]:
    document = _document()
    document["containers"]["mcp"].pop("package_dir")
    document["containers"]["mcp"].update(container_extra)
    if kind_resource:
        document = yaml.safe_load((PACKAGES / "minio" / "keystone-service.yaml").read_text())
        role = document["provides"]["container"]
        document["containers"][role].update(container_extra)
    return manifest_errors(document)


@pytest.mark.parametrize("source", [".", "provider", "providers/atlassian"])
def test_schema_accepts_package_dir(source: str) -> None:
    assert _errors({"package_dir": {"from": source, "path": "/srv/provider"}}) == []


def test_schema_accepts_package_dir_on_init_container() -> None:
    document = _document()
    document["containers"]["seed"] = {
        "image": document["containers"]["mcp"]["image"],
        "init": True,
        "package_dir": {"from": ".", "path": "/seed"},
    }
    assert manifest_errors(document) == []


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ({"from": "/etc", "path": "/srv/provider"}, "containers.mcp.package_dir.from"),
        ({"from": "../outside", "path": "/srv/provider"}, "containers.mcp.package_dir.from"),
        ({"from": "provider/../..", "path": "/srv/provider"}, "containers.mcp.package_dir.from"),
        ({"from": "./provider", "path": "/srv/provider"}, "containers.mcp.package_dir.from"),
        ({"from": "provider//x", "path": "/srv/provider"}, "containers.mcp.package_dir.from"),
        ({"from": "provider", "path": "relative"}, "containers.mcp.package_dir.path"),
        ({"from": "provider", "path": "/srv:/host"}, "containers.mcp.package_dir.path"),
        ({"from": "provider", "path": "/srv", "host": "/etc"}, "additional properties"),
        ({"from": "provider"}, "'path' is a required property"),
    ],
)
def test_schema_rejects_bad_package_dir(value: dict, needle: str) -> None:
    errors = _errors({"package_dir": value})
    assert errors
    assert any(needle in line for line in errors), errors


def test_schema_rejects_a_second_package_dir_on_one_container() -> None:
    two = [
        {"from": "provider", "path": "/srv/a"},
        {"from": "other", "path": "/srv/b"},
    ]
    errors = _errors({"package_dir": two})
    assert any(line.startswith("containers.mcp.package_dir") for line in errors), errors
    errors = _errors(
        {
            "package_dir": {"from": "provider", "path": "/srv/a"},
            "package_dirs": {"from": "other", "path": "/srv/b"},
        }
    )
    assert any("additional properties are not allowed: package_dirs" in line for line in errors)


def test_package_dir_path_must_differ_from_a_volume_path() -> None:
    errors = _errors(
        {
            "volumes": [{"name": "data", "path": "/srv/provider"}],
            "package_dir": {"from": "provider", "path": "/srv/provider"},
        }
    )
    assert any("also the path of a volume" in line for line in errors), errors


def test_package_dir_is_refused_on_a_resource() -> None:
    errors = _errors({"package_dir": {"from": ".", "path": "/pkg"}}, kind_resource=True)
    assert any("supported only for kind: service" in line for line in errors), errors


def test_old_manifests_stay_valid() -> None:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    assert manifest_errors(document) == []


# lint


def _lint_dir(tmp_path: Path, source: str = "provider") -> Path:
    directory = tmp_path / "pkg"
    directory.mkdir()
    document = _document(source=source)
    document.pop("recommended_policy", None)
    (directory / "keystone-service.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (directory / "provider").mkdir()
    (directory / "provider" / "provider.yaml").write_text("name: demo\n", encoding="utf-8")
    return directory


def _lint(capsys: pytest.CaptureFixture[str], directory: Path) -> tuple[int, dict]:
    code = main(["--json", "lint", str(directory)])
    return code, json.loads(capsys.readouterr().out)


def _messages(document: dict) -> list[str]:
    return [item["message"] for item in document["result"]["problems"]]


def test_lint_accepts_plain_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _lint(capsys, _lint_dir(tmp_path))
    assert code == 0, document


def test_lint_rejects_missing_from(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _lint_dir(tmp_path, source="absent")
    code, document = _lint(capsys, directory)
    assert code == 3
    assert document["error"]["id"] == "manifest_invalid"
    assert any("absent does not exist in the package" in line for line in _messages(document))


def test_lint_rejects_symlink_in_subtree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _lint_dir(tmp_path)
    secret = tmp_path / "host-secret"
    secret.write_text("not for the container\n", encoding="utf-8")
    (directory / "provider" / "token").symlink_to(secret)
    code, document = _lint(capsys, directory)
    assert code == 3
    assert document["error"]["id"] == "manifest_invalid"
    assert any("provider/token is a symbolic link" in line for line in _messages(document))


def test_lint_rejects_from_that_is_a_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _lint_dir(tmp_path)
    shutil.rmtree(directory / "provider")
    outside = tmp_path / "host-dir"
    outside.mkdir()
    (outside / "passwd").write_text("root\n", encoding="utf-8")
    (directory / "provider").symlink_to(outside)
    code, document = _lint(capsys, directory)
    assert code == 3
    assert any("provider is a symbolic link" in line for line in _messages(document))


def test_lint_rejects_hard_link(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _lint_dir(tmp_path)
    os.link(directory / "provider" / "provider.yaml", directory / "provider" / "again.yaml")
    code, document = _lint(capsys, directory)
    assert code == 3
    assert any("is a hard link" in line for line in _messages(document))


@pytest.mark.skipif(_MKFIFO is None, reason="no FIFOs on this platform")
def test_lint_rejects_fifo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _lint_dir(tmp_path)
    assert _MKFIFO is not None
    _MKFIFO(directory / "provider" / "pipe")
    code, document = _lint(capsys, directory)
    assert code == 3
    assert any(
        "provider/pipe is not a regular file or directory" in line for line in _messages(document)
    )


def test_lint_rejects_size_over_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _lint_dir(tmp_path)
    (directory / "provider" / "big.bin").write_bytes(b"x" * MAX_BYTES)
    code, document = _lint(capsys, directory)
    assert code == 3
    assert any("the limit is 921600 bytes" in line for line in _messages(document))


def test_lint_rejects_too_many_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _lint_dir(tmp_path)
    for index in range(MAX_FILES):
        (directory / "provider" / f"f{index}.txt").write_text("x", encoding="utf-8")
    code, document = _lint(capsys, directory)
    assert code == 3
    assert any(f"the limit is {MAX_FILES}" in line for line in _messages(document))


# Hash and copy


def _tree(base: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return base


def test_digest_covers_paths_and_bytes(tmp_path: Path) -> None:
    one = _tree(tmp_path / "one", {"a.txt": "1", "d/b.txt": "2"})
    two = _tree(tmp_path / "two", {"a.txt": "1", "d/b.txt": "2"})
    renamed = _tree(tmp_path / "three", {"a.txt": "1", "d/c.txt": "2"})
    changed = _tree(tmp_path / "four", {"a.txt": "1", "d/b.txt": "3"})
    assert subtree_digest(one, ".") == subtree_digest(two, ".")
    assert subtree_digest(one, ".") != subtree_digest(renamed, ".")
    assert subtree_digest(one, ".") != subtree_digest(changed, ".")


def _copy(root: Path, cache: Path, document: dict | None = None) -> tuple[dict, list[str]]:
    manifest = document or _document()
    planned = plan(manifest, cache, ".")
    return planned, ensure_copies(root, manifest, cache, ".", planned)


def test_copy_modes_reuse_and_new_copy(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        root = tmp_path / "root"
        root.mkdir()
        cache = _tree(tmp_path / "cache", PROVIDER)
        os.chmod(cache / "provider" / "provider.yaml", 0o600)
        planned, created = _copy(root, cache)
    finally:
        os.umask(previous)
    copy = planned["mcp"]["copy"]
    assert created == [copy]
    assert copy.startswith(".keystonectl/package-dirs/file-mcp/1.0.0-")
    target = root / copy
    base = root / ".keystonectl" / "package-dirs"
    for directory in (base, base / "file-mcp", target, target / "lib"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory
    for path in (target / "provider.yaml", target / "lib" / "requirements.txt"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, path
    assert (target / "provider.yaml").read_text(encoding="utf-8") == "name: demo\n"
    assert stat.S_IMODE((root / ".keystonectl").stat().st_mode) == 0o700

    inode = target.stat().st_ino
    again, created = _copy(root, cache)
    assert again == planned
    assert created == []
    assert target.stat().st_ino == inode

    (cache / "provider" / "provider.yaml").write_text("name: changed\n", encoding="utf-8")
    newer, created = _copy(root, cache)
    assert newer["mcp"]["copy"] != copy
    assert created == [newer["mcp"]["copy"]]
    assert sorted(_copies(root)) == sorted([copy.rsplit("/", 1)[1], created[0].rsplit("/", 1)[1]])


def test_copy_rechecks_links(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    cache = _tree(tmp_path / "cache", PROVIDER)
    planned = plan(_document(), cache, ".")
    secret = tmp_path / "host-secret"
    secret.write_text("x\n", encoding="utf-8")
    (cache / "provider" / "token").symlink_to(secret)
    with pytest.raises(PackageDirError) as caught:
        ensure_copies(root, _document(), cache, ".", planned)
    assert "symbolic link" in caught.value.messages[0]
    assert _copies(root) == []


def test_damaged_copy_is_replaced(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    cache = _tree(tmp_path / "cache", PROVIDER)
    planned, _created = _copy(root, cache)
    target = root / planned["mcp"]["copy"]
    shutil.rmtree(target)
    target.mkdir()
    _again, created = _copy(root, cache)
    assert created == [planned["mcp"]["copy"]]
    assert (target / "provider.yaml").is_file()


def test_fragment_mount_line_and_injected_paths_ignored() -> None:
    document = _document()
    copy = ".keystonectl/package-dirs/file-mcp/1.0.0-0123456789ab"
    entry = {"package_dirs": {"mcp": {"from": "provider", "path": "/srv/provider", "copy": copy}}}
    text = render_fragment(document, entry)
    assert f"      - ../{copy}:/srv/provider:ro\n" in text
    for bad in ("/etc", "../../etc", ".keystonectl/package-dirs/other/1.0.0-0123456789ab"):
        entry["package_dirs"]["mcp"]["copy"] = bad
        assert ":/srv/provider:ro" not in render_fragment(document, entry)


def test_package_without_package_dir_is_unchanged() -> None:
    document = _document()
    document["containers"]["mcp"].pop("package_dir")
    assert render_fragment(document, {}) == render_fragment(
        document, {"package_dirs": {"mcp": {"copy": "x"}}}
    )


# Apply and lifecycle


def test_apply_writes_copy_fragment_and_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = _apply(root)
    assert result.code == 0, result.error
    entry = load_lock(root / "services.lock.yaml")["services"][0]
    dirs = entry["package_dirs"]
    copy = dirs["mcp"]["copy"]
    assert dirs["mcp"]["from"] == "provider"
    assert dirs["mcp"]["path"] == "/srv/provider"
    assert (root / copy / "provider.yaml").is_file()
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"- ../{copy}:/srv/provider:ro" in fragment
    import hashlib

    assert entry["fragment_sha256"] == hashlib.sha256(fragment.encode()).hexdigest()


def test_plan_does_not_copy(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    root = tmp_path / "root"
    catalog = prepare_root(root, {"file-mcp": _text()})
    result = run_service(
        root,
        "plan",
        "file-mcp",
        runner=runner_for(
            {"file-mcp": _text()}, DockerBox(), catalog, package_files={"file-mcp": PROVIDER}
        ),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 0, result.error
    assert not (root / ".keystonectl" / "package-dirs").exists()


def test_apply_refuses_link_in_package_tree(tmp_path: Path) -> None:
    from keystonectl.generate.install import ComposeDeployer
    from keystonectl.plan import run_service
    from tests.support.deploylab import prepare_root, runner_for

    root = tmp_path / "root"
    catalog = prepare_root(root, {"file-mcp": _text()})
    runner = runner_for(
        {"file-mcp": _text()}, DockerBox(), catalog, package_files={"file-mcp": PROVIDER}
    )
    clone = {tuple(prefix): outcome for prefix, outcome in runner.rules}[("git", "clone")]

    def link_after_clone(args, **kwargs):
        result = clone(args, **kwargs)
        if "catalog" not in str(args[-2]):
            (Path(args[-1]) / "provider" / "token").symlink_to("/etc/hostname")
        return result

    runner.rules = [
        (prefix, link_after_clone if tuple(prefix) == ("git", "clone") else outcome)
        for prefix, outcome in runner.rules
    ]
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        defer_public=True,
    )
    assert result.code == 3
    assert result.error["id"] == "manifest_invalid"
    assert "symbolic link" in result.error["message"]
    assert _copies(root) == []
    assert not (root / "packages" / "file-mcp.yml").exists()


def test_failed_first_apply_leaves_no_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr("keystonectl.generate.install._monotonic", lambda: clock["t"])

    def advance(_seconds: float = 3) -> None:
        clock["t"] += 10000

    monkeypatch.setattr("keystonectl.generate.install.pause", advance)
    box = DockerBox()
    box.inspect_health = "unhealthy"
    root = tmp_path / "root"
    result, _runner, _box = _apply(root, box=box)
    assert result.code == 3
    assert result.error["id"] == "service_unhealthy"
    assert result.result["rollback"]["lock_entry"] == "dropped"
    assert not (root / ".keystonectl" / "package-dirs" / "file-mcp").exists()


def test_check_warns_when_copy_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = _apply(root)
    assert result.code == 0, result.error
    copy = load_lock(root / "services.lock.yaml")["services"][0]["package_dirs"]["mcp"]["copy"]
    checked = run_check(root)
    assert not any(item["id"] == "package_dir_missing" for item in checked.warnings)
    shutil.rmtree(root / copy)
    checked = run_check(root)
    warning = next(item for item in checked.warnings if item["id"] == "package_dir_missing")
    assert copy in warning["message"]
    assert "run service apply file-mcp again" in warning["message"]
    item = next(item for item in checked.result["checks"] if item["id"] == "package_dir_missing")
    assert item["hint"] == "run service apply file-mcp again"


def test_apply_again_restores_a_missing_copy(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = _apply(root)
    assert result.code == 0, result.error
    copy = load_lock(root / "services.lock.yaml")["services"][0]["package_dirs"]["mcp"]["copy"]
    shutil.rmtree(root / copy)
    again, runner, _box = apply_package(
        root,
        "file-mcp",
        {"file-mcp": _text()},
        reset=False,
        package_files={"file-mcp": PROVIDER},
    )
    assert again.code == 0, again.error
    assert (root / copy / "provider.yaml").is_file()
    ups = [call.args for call in runner.calls if "up" in call.args and "-d" in call.args]
    assert ups and "--force-recreate" in ups[-1]


def test_package_without_package_dir_gets_no_copies(tmp_path: Path) -> None:
    document = _document()
    document["containers"]["mcp"].pop("package_dir")
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": yaml.safe_dump(document)})
    assert result.code == 0, result.error
    assert "package_dirs" not in load_lock(root / "services.lock.yaml")["services"][0]
    assert not (root / ".keystonectl" / "package-dirs").exists()


def _release_runner(
    root: Path, releases: dict[str, tuple[str, dict[str, str]]]
) -> FakeCommandRunner:
    """Fake git and Docker. ``releases`` maps a tag to the manifest text and package files."""

    box = DockerBox()

    def ls_remote(args, **_kwargs):
        tag = "v1.0.0"
        for item in args:
            if str(item).startswith("refs/tags/"):
                tag = str(item).removeprefix("refs/tags/")
        return CommandResult(tuple(args), 0, f"{SHA}\trefs/tags/{tag}\n", "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        if "catalog" in str(args[-2]):
            catalog = root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml"
            (dest / "catalog.yaml").write_text(catalog.read_text(encoding="utf-8"))
            return CommandResult(tuple(args), 0, "", "")
        tag = args[args.index("--branch") + 1]
        text, files = releases[tag]
        (dest / "keystone-service.yaml").write_text(text, encoding="utf-8")
        _tree(dest, files)
        (dest / ".git").mkdir(exist_ok=True)
        return CommandResult(tuple(args), 0, "", "")

    ok = CommandResult((), 0, "", "")
    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
            (["git"], ok),
            (["docker"], box),
        ]
    )


def _add_release(root: Path, version: str, files: dict[str, str]) -> tuple[str, dict[str, str]]:
    catalog_path = root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog["packages"]["file-mcp"]["versions"].append(
        {"version": version, "ref": f"v{version}", "keystone_mcp": ">=0.5.0,<2.0"}
    )
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    text = _text(version=version)
    lock = load_lock(root / "services.lock.yaml")
    cache = package_cache(root, lock["services"][0]["source"], "file-mcp", version)
    cache.mkdir(parents=True)
    (cache / "keystone-service.yaml").write_text(text, encoding="utf-8")
    return text, files


def _upgrade(root: Path, version: str, releases: dict) -> object:
    return upgrade_service(
        root,
        f"file-mcp@{version}",
        runner=_release_runner(root, releases),
        secrets=SecretRegistry(),
        sets={pkg_var("file-mcp", "CONFIRM_UPGRADE"): version},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
    )


def _entry(root: Path) -> dict:
    return load_lock(root / "services.lock.yaml")["services"][0]


def _name(copy: str) -> str:
    return copy.rsplit("/", 1)[1]


def test_upgrade_rollback_remove_keep_exactly_the_expected_copies(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = _apply(root)
    assert result.code == 0, result.error
    first = _entry(root)["package_dirs"]["mcp"]["copy"]
    releases = {"v1.0.0": (_text(), PROVIDER)}

    releases["v1.1.0"] = _add_release(root, "1.1.0", {"provider/provider.yaml": "name: v11\n"})
    upgraded = _upgrade(root, "1.1.0", releases)
    assert upgraded.code == 0, upgraded.error
    entry = _entry(root)
    second = entry["package_dirs"]["mcp"]["copy"]
    assert second.startswith(".keystonectl/package-dirs/file-mcp/1.1.0-")
    assert entry["previous"]["package_dirs"]["mcp"]["copy"] == first
    assert _copies(root) == sorted([_name(first), _name(second)])
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"../{second}:/srv/provider:ro" in fragment

    releases["v1.2.0"] = _add_release(root, "1.2.0", {"provider/provider.yaml": "name: v12\n"})
    upgraded = _upgrade(root, "1.2.0", releases)
    assert upgraded.code == 0, upgraded.error
    third = _entry(root)["package_dirs"]["mcp"]["copy"]
    assert _copies(root) == sorted([_name(second), _name(third)])

    ok = FakeCommandRunner([([], lambda args, **_k: CommandResult(tuple(args), 0, "", ""))])
    rolled = rollback_service(root, "file-mcp", runner=ok, clock=lambda: CLOCK)
    assert rolled.code == 0, rolled.error
    entry = _entry(root)
    assert entry["previous"] is None
    assert entry["package_dirs"]["mcp"]["copy"] == second
    assert _copies(root) == [_name(second)]
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"../{second}:/srv/provider:ro" in fragment
    assert run_check(root).code == 0

    removed = remove_service(
        root,
        "file-mcp",
        runner=ok,
        secrets=SecretRegistry(),
        sets={pkg_var("file-mcp", "CONFIRM_REMOVE"): "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert removed.code == 0, removed.error
    assert not (root / ".keystonectl" / "package-dirs" / "file-mcp").exists()
    assert not any(item["id"] == "package_dir_kept" for item in removed.warnings)


def test_rollback_keeps_the_newer_copy_when_recreate_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert _apply(root)[0].code == 0
    releases = {"v1.0.0": (_text(), PROVIDER)}
    releases["v1.1.0"] = _add_release(root, "1.1.0", {"provider/provider.yaml": "name: v11\n"})
    assert _upgrade(root, "1.1.0", releases).code == 0
    failing = FakeCommandRunner([([], lambda args, **_k: CommandResult(tuple(args), 1, "", "x"))])
    rolled = rollback_service(root, "file-mcp", runner=failing, clock=lambda: CLOCK)
    assert rolled.code == 0
    assert len(_copies(root)) == 2


def test_eject_keeps_copy_and_remove_warns(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert _apply(root)[0].code == 0
    copy = _entry(root)["package_dirs"]["mcp"]["copy"]
    assert eject_service(root, "file-mcp", clock=lambda: CLOCK).code == 0
    assert (root / copy).is_dir()
    assert _entry(root)["package_dirs"]["mcp"]["copy"] == copy
    ok = FakeCommandRunner([([], lambda args, **_k: CommandResult(tuple(args), 0, "", ""))])
    removed = remove_service(
        root,
        "file-mcp",
        runner=ok,
        secrets=SecretRegistry(),
        sets={pkg_var("file-mcp", "CONFIRM_REMOVE"): "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
    )
    assert removed.code == 0, removed.error
    assert (root / copy / "provider.yaml").is_file()
    assert ".keystonectl/package-dirs/file-mcp" in removed.result["kept"]
    warning = next(item for item in removed.warnings if item["id"] == "package_dir_kept")
    assert "packages/file-mcp.yml" in warning["message"]


def test_lock_schema_rejects_a_host_path_as_copy(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert _apply(root)[0].code == 0
    lock = load_lock(root / "services.lock.yaml")
    lock["services"][0]["package_dirs"]["mcp"]["copy"] = "/etc"
    with pytest.raises(Exception, match="package_dirs"):
        save_lock(root / "services.lock.yaml", lock, now=CLOCK)


# docker compose resolves the copy path from packages/


def _compose_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "root"
    result, _runner, _box = _apply(root)
    assert result.code == 0, result.error
    copy = _entry(root)["package_dirs"]["mcp"]["copy"]
    (root / "docker-compose.yml").write_text(
        "services:\n  keystone:\n    image: keystone-mcp:0.5.0\n", encoding="utf-8"
    )
    return root, copy


def _bind_source(config: dict, service: str, target: str) -> str:
    for volume in config["services"][service]["volumes"]:
        if volume.get("target") == target:
            assert volume["type"] == "bind"
            assert volume["read_only"] is True
            return str(volume["source"])
    raise AssertionError(f"no mount at {target}")


def test_compose_config_resolves_copy(tmp_path: Path) -> None:
    root, copy = _compose_root(tmp_path)
    expected = os.path.realpath(root / copy)
    docker = shutil.which("docker")
    rendered: dict | None = None
    if docker:
        try:
            completed = subprocess.run(
                [
                    docker,
                    "compose",
                    "--project-directory",
                    str(root),
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
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            rendered = json.loads(completed.stdout)
    if rendered is not None:
        source = _bind_source(rendered, "file-mcp-mcp", "/srv/provider")
        assert os.path.realpath(source) == expected
        return
    fragment_path = root / "packages" / "file-mcp.yml"
    fragment = yaml.safe_load(fragment_path.read_text(encoding="utf-8"))
    line = next(
        item
        for item in fragment["services"]["file-mcp-mcp"]["volumes"]
        if item.endswith(":/srv/provider:ro")
    )
    source = line.split(":", 1)[0]
    assert os.path.realpath(fragment_path.parent / source) == expected
