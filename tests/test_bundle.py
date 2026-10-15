"""bundle plan and install: order, answers, shared resources, and reruns."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.bundles import _manifest_cached, install_bundle
from keystonectl.output import SecretRegistry
from tests.support import file_hashes
from tests.support.deploylab import LAYOUT
from tests.support.resources import (
    Lab,
    install_ok_probes,
    pg_consumer,
    pg_provider,
    s3_consumer,
    standard_manifests,
)
from tests.support.served_catalog import catalog_runner


def _root(tmp_path: Path) -> Path:
    from tests.support import write_empty_lock

    write_empty_lock(tmp_path)
    lock = yaml.safe_load((tmp_path / "services.lock.yaml").read_text(encoding="utf-8"))
    lock["layout"] = LAYOUT
    (tmp_path / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "name": "keystone-codes",
                        "url": "https://example.com/keystone-codes.git",
                        "ref": "v1.0.0",
                        "priority": 100,
                    },
                    {
                        "name": "partner",
                        "url": "https://example.com/partner.git",
                        "ref": "v1.0.0",
                        "priority": 90,
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "keystonectl").mkdir()
    (tmp_path / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries: []\n", encoding="utf-8"
    )
    (tmp_path / "keystonectl" / "catalog-snapshot.yaml").write_text(
        "active: []\nreserved: []\nwip: []\n", encoding="utf-8"
    )
    return tmp_path


def _run(
    root: Path, ref: str, *, plan_only: bool = True, environ: dict | None = None, deployer=None
):
    return install_bundle(
        root,
        ref,
        plan_only=plan_only,
        runner=catalog_runner(),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0", **(environ or {})},
        interactive=False,
        deployer=deployer,
        defer_public=True,
    )


def test_bundle_order_by_requires(tmp_path: Path) -> None:
    result = _run(_root(tmp_path), "keystone-codes/docs")
    assert result.result["order"] == ["file-mcp", "docs-service"]


def test_bundle_requires_cycle(tmp_path: Path) -> None:
    alpha = s3_consumer("alpha", namespace="alpha")
    beta = s3_consumer("beta", namespace="beta")
    alpha["requires"] = ["beta"]
    beta["requires"] = ["alpha"]
    lab = Lab(tmp_path, [alpha, beta])
    _put_bundle(lab, "loop", ["alpha", "beta"])
    result = install_bundle(
        lab.root,
        "local/loop",
        plan_only=True,
        runner=lab.runner,
        secrets=lab.secrets,
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        deployer=lab.deployer,
        defer_public=True,
    )
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "requires_cycle"
    assert "alpha" in result.error["message"]
    assert "beta" in result.error["message"]


def test_bundle_packages_only_from_bundle_source(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _run(root, "keystone-codes/minimal")
    assert (root / ".keystonectl" / "packages" / "keystone-codes" / "file-mcp" / "1.0.0").is_dir()
    assert not (root / ".keystonectl" / "packages" / "partner" / "file-mcp").exists()
    assert result.result["order"] == ["file-mcp"]


def test_bundle_ambiguous_name_asks_full_name(tmp_path: Path) -> None:
    result = _run(_root(tmp_path), "minimal")
    assert result.code == 2
    assert result.missing[0]["var"] == "bundle"
    assert result.missing[0]["kind"] == "argument"
    assert result.missing[0]["choices"] == ["keystone-codes/minimal", "partner/minimal"]


def test_bundle_not_found(tmp_path: Path) -> None:
    result = _run(_root(tmp_path), "missing")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "bundle_not_found"


def test_bundle_stops_at_first_missing_with_statuses(tmp_path: Path) -> None:
    result = _run(_root(tmp_path), "keystone-codes/docs")
    assert result.code == 2
    packages = {item["name"]: item["status"] for item in result.result["packages"]}
    assert packages["file-mcp"] == "needs_answers"
    assert packages["docs-service"] == "not_started"
    assert result.missing


def test_bundle_answers_for_later_packages_taken_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    docs = pg_consumer("docs-service", namespace="docs")
    notes = pg_consumer("notes-mcp")
    lab = Lab(tmp_path, [notes, docs, pg_provider()])
    _put_bundle(lab, "docs", ["notes-mcp", "docs-service"])
    env = {
        **lab.pg_answers(),
        "KEYSTONE_SETUP_SERVICE_DOCS_SERVICE_POSTGRES_DATABASE": "from-env-db",
    }
    result = install_bundle(
        lab.root,
        "local/docs",
        plan_only=False,
        runner=lab.runner,
        secrets=lab.secrets,
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0", **env},
        interactive=False,
        deployer=lab.deployer,
        defer_public=True,
    )
    assert result.code == 0, result.error
    statuses = {item["name"]: item["status"] for item in result.result["packages"]}
    assert statuses["docs-service"] == "installed"
    text = (lab.root / "packages" / "docs-service.env").read_text(encoding="utf-8")
    assert "NOTES_PG_DATABASE=from-env-db" in text
    assert "KEYSTONE_SETUP_SERVICE_DOCS_SERVICE_POSTGRES_DATABASE" not in {
        item["var"] for item in result.missing
    }


def test_bundle_rerun_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(
        tmp_path,
        [s3_consumer(), s3_consumer("pages-mcp", namespace="pages"), *standard_manifests()[5:]],
    )
    _put_bundle(lab, "pair", ["file-mcp", "pages-mcp"])
    state = {"fail": True}
    original = lab.runner.run

    def run(args, **kwargs):
        if (
            state["fail"]
            and list(args[:2]) == ["docker", "run"]
            and "--consumer" in args
            and args[list(args).index("--consumer") + 1] == "pages-mcp"
        ):
            from keystonectl.runner import CommandResult

            return CommandResult(tuple(args), 1, "", "provision failed")
        return original(args, **kwargs)

    lab.runner.run = run  # type: ignore[method-assign]
    first = _install(lab, "local/pair", {**lab_env_catalog()})
    assert first.code == 3, first.error
    statuses = {item["name"]: item["status"] for item in first.result["packages"]}
    assert statuses["file-mcp"] == "installed"
    assert statuses["pages-mcp"] == "failed"
    state["fail"] = False
    second = _install(lab, "local/pair", {**lab_env_catalog()})
    assert second.code == 0, second.error
    again = {item["name"]: item["status"] for item in second.result["packages"]}
    assert again["file-mcp"] == "unchanged"
    assert again["pages-mcp"] == "installed"


def test_bundle_rerun_all_unchanged_changes_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, [s3_consumer(), *standard_manifests()[5:]])
    _put_bundle(lab, "minimal", ["file-mcp"])
    answers = lab_env_catalog()
    first = _install(lab, "local/minimal", answers)
    assert first.code == 0, first.error
    second = _install(lab, "local/minimal", answers)
    assert second.code == 0, second.error
    assert second.changes == 0
    assert second.result["packages"][0]["status"] == "unchanged"


def test_bundle_shared_resource_asked_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(
        tmp_path,
        [s3_consumer(), s3_consumer("pages-mcp", namespace="pages"), *standard_manifests()[5:]],
    )
    _put_bundle(lab, "pair", ["file-mcp", "pages-mcp"])
    result = _install(lab, "local/pair", lab_env_catalog())
    assert result.code == 0, result.error
    assert "KEYSTONE_SETUP_RESOURCE_S3" not in {item["var"] for item in result.missing}
    first = (lab.root / "packages" / "file-mcp.env").read_text(encoding="utf-8")
    second = (lab.root / "packages" / "pages-mcp.env").read_text(encoding="utf-8")
    assert "FILE_MCP_S3_BUCKET=file-mcp" in first
    assert "FILE_MCP_S3_BUCKET=pages-mcp" in second


def test_bundle_plan_changes_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    before = file_hashes(root)
    result = _run(root, "keystone-codes/minimal")
    assert result.code == 2
    after = file_hashes(root)
    kept = {key: value for key, value in before.items() if not key.startswith(".keystonectl/")}
    for key, value in kept.items():
        assert after.get(key) == value


def _seed_cached_manifest(
    root: Path, source: str, name: str, version: str, *, policy: str, critical: str
) -> None:
    directory = root / ".keystonectl" / "packages" / source / name / version
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "recommended_policy": policy,
        "mcp": {"namespace": "files", "critical": [critical]},
    }
    (directory / "keystone-service.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )


def _bundle_plan(lab: Lab, name: str, packages: list[str], *, environ: dict[str, str]) -> object:
    _put_bundle(lab, name, packages)
    return install_bundle(
        lab.root,
        f"local/{name}",
        plan_only=True,
        runner=lab.runner,
        secrets=lab.secrets,
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0", **environ},
        interactive=False,
        deployer=lab.deployer,
        defer_public=True,
    )


def test_cached_manifest_sorted_as_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    _seed_cached_manifest(
        lab.root, "local", "file-mcp", "1.9.0", policy="policy-1.9.yaml", critical="old"
    )
    _seed_cached_manifest(
        lab.root, "local", "file-mcp", "1.10.0", policy="policy-1.10.yaml", critical="new"
    )
    result = _bundle_plan(
        lab,
        "solo",
        ["file-mcp"],
        environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"},
    )
    assert result.code == 0, result.error
    row = next(
        item for item in result.result["recommended_policy"] if item["package"] == "file-mcp"
    )
    assert row["path"] == "policy-1.10.yaml"
    critical = next(
        item for item in result.result["critical_tools"] if item["package"] == "file-mcp"
    )
    assert critical["name"] == "files_new"


def test_cached_manifest_prefers_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    installed = lab.apply("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"})
    assert installed.code == 0, installed.error
    _seed_cached_manifest(
        lab.root, "local", "file-mcp", "1.0.0", policy="policy-1.0.yaml", critical="v0"
    )
    _seed_cached_manifest(
        lab.root, "local", "file-mcp", "1.1.0", policy="policy-1.1.yaml", critical="v1"
    )
    lock = yaml.safe_load((lab.root / "services.lock.yaml").read_text(encoding="utf-8"))
    manifest = _manifest_cached(lab.root, "local", "file-mcp", lock)
    assert manifest is not None
    assert manifest["recommended_policy"] == "policy-1.0.yaml"


def test_cached_manifest_skips_non_version_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    _seed_cached_manifest(
        lab.root, "local", "file-mcp", "1.0.0", policy="policy-1.0.yaml", critical="ok"
    )
    base = lab.root / ".keystonectl" / "packages" / "local" / "file-mcp"
    (base / "tmp-xyz").mkdir(parents=True)
    lock = yaml.safe_load((lab.root / "services.lock.yaml").read_text(encoding="utf-8"))
    manifest = _manifest_cached(lab.root, "local", "file-mcp", lock)
    assert manifest is not None
    assert manifest["recommended_policy"] == "policy-1.0.yaml"


def test_bundle_recommended_policy_merged_with_package_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    docs = pg_consumer("docs-service", namespace="docs")
    docs["requires"] = ["file-mcp"]
    docs["mcp"]["tools"] = ["search_docs", "read_page"]
    docs["mcp"]["critical"] = ["search_docs"]
    docs["recommended_policy"] = "policy.example.yaml"
    lab = Lab(tmp_path, [s3_consumer(), docs, *standard_manifests()[5:]])
    _put_bundle(lab, "docs", ["docs-service", "file-mcp"])
    result = install_bundle(
        lab.root,
        "local/docs",
        plan_only=True,
        runner=lab.runner,
        secrets=lab.secrets,
        sets={},
        environ={
            "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0",
            **lab.s3_answers(),
            **lab.pg_answers(),
        },
        interactive=False,
        deployer=lab.deployer,
        defer_public=True,
    )
    assert result.code == 0, result.error
    packages = {item["package"] for item in result.result["recommended_policy"]}
    assert packages == {"file-mcp", "docs-service"}
    critical = {(item["package"], item["name"]) for item in result.result["critical_tools"]}
    assert ("file-mcp", "files_create_upload_url") in critical
    assert ("docs-service", "docs_search_docs") in critical


def _put_bundle(lab: Lab, name: str, packages: list[str]) -> None:
    url = "https://example.com/catalog.git"
    catalog = yaml.safe_load(lab.by_url[url])
    catalog.setdefault("bundles", {})[name] = {
        "description": f"Test bundle {name}",
        "packages": packages,
    }
    text = yaml.safe_dump(catalog, sort_keys=False)
    lab.by_url[url] = text
    cache = lab.root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml"
    cache.write_text(text, encoding="utf-8")


def lab_env_catalog() -> dict[str, str]:
    return {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}


def _install(lab: Lab, ref: str, environ: dict[str, str]):
    return install_bundle(
        lab.root,
        ref,
        plan_only=False,
        runner=lab.runner,
        secrets=lab.secrets,
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0", **environ},
        interactive=False,
        deployer=lab.deployer,
        defer_public=True,
    )
