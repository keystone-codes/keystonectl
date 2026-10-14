"""Download a git tag or commit. Callers pass a CommandRunner; git never prompts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from keystonectl.runner import CommandResult, CommandRunner

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}
GIT_TIMEOUT = 60.0
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MODE_PRIVATE_DIR = 0o700


class FetchError(Exception):
    def __init__(self, error_id: str, message: str) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message


def is_commit_sha(ref: str) -> bool:
    return _SHA.fullmatch(ref) is not None


def strip_userinfo(url: str) -> str:
    """Drop ``user:token@`` from a URL before it is shown to the operator."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def describe_fetch_error(subject: str, exc: FetchError) -> str:
    """Prefix ``tag_not_found`` messages with the source or package they came from."""
    if exc.error_id == "tag_not_found":
        return f"{subject}: {exc.message}"
    return exc.message


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(runner: CommandRunner, args: list[str], *, cwd: Path | None = None) -> CommandResult:
    return runner.run(args, env=GIT_ENV, cwd=cwd, timeout=GIT_TIMEOUT)


def _fail_result(result: CommandResult, action: str) -> FetchError:
    detail = (result.stderr or result.stdout or action).strip()
    return FetchError("source_unreachable", detail or action)


def tag_on_remote(runner: CommandRunner, url: str, tag: str) -> None:
    """Refuse when ``tag`` is not a tag. A branch of the same name is not a tag."""
    result = _run(runner, ["git", "ls-remote", "--tags", url, f"refs/tags/{tag}"])
    if result.returncode != 0:
        raise _fail_result(result, "git ls-remote failed")
    needle = f"refs/tags/{tag}"
    for line in result.stdout.splitlines():
        ref = line.split("\t", 1)[-1] if "\t" in line else ""
        if ref == needle or ref == f"{needle}^{{}}":
            return
    raise FetchError("tag_not_found", f"Tag {tag} was not found at {strip_userinfo(url)}.")


def list_tags(runner: CommandRunner, url: str) -> str:
    result = _run(runner, ["git", "ls-remote", "--tags", url])
    if result.returncode != 0:
        raise _fail_result(result, "git ls-remote failed")
    return result.stdout


def _tighten(path: Path, *, created: bool) -> None:
    """Chmod ``path`` to 700 when it was just created or its mode is wider than 700."""
    if created or (path.stat().st_mode & 0o077):
        os.chmod(path, _MODE_PRIVATE_DIR)


def _tmp_root(root: Path) -> Path:
    """``.keystonectl/tmp`` next to the cache, created with mode 700 if absent.

    Clones are built here so the later rename into the cache or into place stays on the
    same file system; ``/tmp`` can be a different one (tmpfs, a separate disk), which
    makes ``Path.rename`` fail with ``EXDEV``.

    ``.keystonectl`` itself is created here, mode 700, if the caller never ran
    ``init`` (for example ``catalog list --root`` on a bare checkout); the deployment
    layout promises mode 700 and a ``.gitignore`` with a single ``*`` line for that
    directory regardless of who creates it first.
    """
    base = Path(root) / ".keystonectl"
    created_base = not base.is_dir()
    base.mkdir(parents=True, exist_ok=True)
    _tighten(base, created=created_base)
    if created_base:
        gitignore = base / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

    tmp = base / "tmp"
    created_tmp = not tmp.is_dir()
    tmp.mkdir(exist_ok=True)
    _tighten(tmp, created=created_tmp)
    return tmp


def fetch_tree(runner: CommandRunner, url: str, ref: str, root: Path) -> tuple[Path, str]:
    """Clone ``ref`` into a new directory with ``.git`` removed. Return ``(path, commit)``.

    The directory is created under ``root/.keystonectl/tmp`` so the caller can later
    rename it onto the cache or deployment directory without crossing file systems.
    """
    tmp = Path(tempfile.mkdtemp(prefix="fetch-", dir=_tmp_root(root)))
    try:
        commit = _materialize(runner, url, ref, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    git_dir = tmp / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)
    return tmp, commit


def _materialize(runner: CommandRunner, url: str, ref: str, dest: Path) -> str:
    if is_commit_sha(ref):
        init = _run(runner, ["git", "init", str(dest)])
        if init.returncode != 0:
            raise _fail_result(init, "git init failed")
        fetched = _run(runner, ["git", "fetch", "--depth", "1", url, ref], cwd=dest)
        if fetched.returncode != 0:
            raise _fail_result(fetched, "git fetch failed")
        checkout = _run(runner, ["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest)
        if checkout.returncode != 0:
            raise _fail_result(checkout, "git checkout failed")
    else:
        tag_on_remote(runner, url, ref)
        cloned = _run(
            runner,
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                "--single-branch",
                "-c",
                "advice.detachedHead=false",
                url,
                str(dest),
            ],
        )
        if cloned.returncode != 0:
            raise _fail_result(cloned, "git clone failed")
    parsed = _run(runner, ["git", "rev-parse", "HEAD"], cwd=dest)
    if parsed.returncode != 0:
        raise _fail_result(parsed, "git rev-parse failed")
    commit = parsed.stdout.strip()
    if not is_commit_sha(commit):
        raise FetchError("source_unreachable", "git rev-parse did not return a commit.")
    return commit


def replace_tree(src: Path, dest: Path) -> None:
    """Move ``src`` onto ``dest``. ``src`` is consumed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = dest.with_name(dest.name + ".replacing")
    if backup.exists():
        shutil.rmtree(backup)
    if dest.exists():
        dest.rename(backup)
    src.rename(dest)
    if backup.exists():
        shutil.rmtree(backup)
