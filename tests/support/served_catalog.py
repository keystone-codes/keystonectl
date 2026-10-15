"""Git runner that serves tests/data/catalogs without a network."""

from __future__ import annotations

from pathlib import Path

from keystonectl.runner import CommandResult, FakeCommandRunner

DATA = Path(__file__).resolve().parents[1] / "data" / "catalogs"
SHA = "a" * 40

URLS = {
    "https://github.com/keystone-codes/keystone-catalog.git": DATA / "keystone-codes",
    "https://example.com/keystone-codes.git": DATA / "keystone-codes",
    "https://example.com/partner.git": DATA / "partner",
    "https://example.com/other.git": DATA / "partner",
    "https://example.com/broken.git": DATA / "broken",
    "https://example.com/keystone-codes/file-mcp.git": DATA
    / "keystone-codes"
    / "packages"
    / "file-mcp.keystone-service.yaml",
    "https://example.com/keystone-codes/docs-service.git": DATA
    / "keystone-codes"
    / "packages"
    / "docs-service.keystone-service.yaml",
    "https://example.com/keystone-codes/minio.git": DATA
    / "keystone-codes"
    / "packages"
    / "minio.keystone-service.yaml",
    "https://example.com/keystone-codes/postgres.git": DATA
    / "keystone-codes"
    / "packages"
    / "postgres.keystone-service.yaml",
    "https://example.com/partner/file-mcp.git": DATA
    / "partner"
    / "packages"
    / "file-mcp.keystone-service.yaml",
}


def fill(dest: Path, spec: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if spec.is_dir():
        (dest / "catalog.yaml").write_text(
            (spec / "catalog.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
    else:
        (dest / "keystone-service.yaml").write_text(
            spec.read_text(encoding="utf-8"), encoding="utf-8"
        )
        policy = spec.parent / "policy.example.yaml"
        if policy.is_file() and "file-mcp" in spec.name:
            (dest / "policy.example.yaml").write_text(
                policy.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if spec.name in {"minio.keystone-service.yaml", "postgres.keystone-service.yaml"}:
            script = spec.parent / "provision.sh"
            text = script.read_text(encoding="utf-8") if script.is_file() else "#!/bin/sh\nexit 0\n"
            (dest / "provision.sh").write_text(text, encoding="utf-8")
    (dest / ".git").mkdir(exist_ok=True)


def catalog_runner(
    *,
    tags: str | None = None,
    fail: bool = False,
) -> FakeCommandRunner:
    listing = tags if tags is not None else f"{SHA}\trefs/tags/v1.0.0\n{SHA}\trefs/tags/v1.2.0\n"

    def ls_remote(args, **_kwargs):
        if fail:
            return CommandResult(tuple(args), 1, "", "down")
        last = str(args[-1])
        if last.startswith("refs/tags/"):
            tag = last.removeprefix("refs/tags/")
            if f"refs/tags/{tag}" not in listing:
                return CommandResult(tuple(args), 0, "", "")
            return CommandResult(tuple(args), 0, f"{SHA}\trefs/tags/{tag}\n", "")
        return CommandResult(tuple(args), 0, listing, "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        url = str(args[-2])
        spec = URLS.get(url)
        if spec is None:
            raise AssertionError(url)
        fill(dest, spec)
        return CommandResult(tuple(args), 0, "", "")

    ok = CommandResult((), 0, "", "")
    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "init"], ok),
            (["git", "fetch"], ok),
            (["git", "checkout"], ok),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
        ]
    )
