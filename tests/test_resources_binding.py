"""Binding needs for s3/v1 and postgres/v1."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.resources.binding import provider_removal_error, unbind_consumer
from tests.support.resources import (
    Lab,
    choice_var,
    consumer_name,
    install_ok_probes,
    pg_consumer,
    pg_provider,
    provider_name,
    s3_consumer,
    s3_provider,
    set_need_version,
    standard_manifests,
)

pytestmark = pytest.mark.parametrize("kind", ["s3", "postgres"])


def test_first_consumer_asks_external_or_catalog(tmp_path: Path, kind: str) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.plan(consumer_name(kind))
    assert result.code == 2
    question = next(item for item in result.missing if item["var"] == choice_var(kind))
    assert question["choices"] == ["external", "catalog"]
    assert question["kind"] == "choice"


def test_two_needs_two_questions(tmp_path: Path, kind: str) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.plan("docs-service")
    assert result.code == 2
    variables = {item["var"] for item in result.missing}
    assert "KEYSTONE_SETUP_RESOURCE_S3" in variables
    assert "KEYSTONE_SETUP_RESOURCE_POSTGRES" in variables


def test_catalog_plans_provider_then_provision(tmp_path: Path, kind: str) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.plan(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert result.code == 0, result.error
    ids = [item["id"] for item in result.actions]
    wanted = ["provider.deploy", "provider.wait_healthy", "consumer.provision", "consumer.probe"]
    positions = [ids.index(item) for item in wanted]
    assert positions == sorted(positions)
    provider = provider_name(kind)
    titles = [item["title"] for item in result.actions]
    assert any(title.startswith(f"Install {provider} 1.0.0 (local)") for title in titles)
    assert any(title == f"Wait until {provider} is healthy" for title in titles)


def test_catalog_several_providers_asks_package(tmp_path: Path, kind: str) -> None:
    extra = s3_provider("garage") if kind == "s3" else pg_provider("pgextra")
    lab = Lab(tmp_path, standard_manifests(), extra=[extra])
    result = lab.plan(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert result.code == 2
    question = next(item for item in result.missing if item["var"] == f"{choice_var(kind)}_PACKAGE")
    other = "garage" if kind == "s3" else "pgextra"
    assert question["choices"] == sorted([f"local/{provider_name(kind)}", f"extra/{other}"])


def test_no_provider_in_catalog(tmp_path: Path, kind: str) -> None:
    documents = [s3_consumer(), pg_consumer()]
    lab = Lab(tmp_path, documents)
    result = lab.plan(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "no_provider"
    assert result.retryable is False


def test_second_consumer_reuses_provider_without_question(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert first.code == 0, first.error
    second_name = "pages-mcp" if kind == "s3" else "url-mcp"
    result = lab.plan(second_name)
    assert result.code == 0, result.error
    variables = {item["var"] for item in result.missing}
    assert choice_var(kind) not in variables
    assert any(item["id"] == "consumer.provision" for item in result.actions)


def test_external_answer_ignored_when_catalog_bound_warns(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind != "s3":
        pytest.skip("catalog-bound S3 scenario")
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"})
    assert first.code == 0, first.error
    result = lab.plan("pages-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "external"})
    assert result.code == 0, result.error
    matches = [item for item in result.warnings if item["id"] == "resource_answer_ignored"]
    assert len(matches) == 1
    assert "minio" in matches[0]["message"]
    assert any(item["id"] == "consumer.provision" for item in result.actions)


def test_catalog_answer_ignored_when_external_bound_warns(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind != "s3":
        pytest.skip("external-bound S3 scenario")
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply("file-mcp", environ=lab.s3_answers())
    assert first.code == 0, first.error
    result = lab.plan("pages-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"})
    assert result.code in (0, 2), result.error
    matches = [item for item in result.warnings if item["id"] == "resource_answer_ignored"]
    assert len(matches) == 1
    assert "external storage" in matches[0]["message"]


def test_matching_answer_does_not_warn(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    if kind == "s3":
        first_env = {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
        second_env = {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    else:
        first_env = {"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"}
        second_env = {"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"}
    first = lab.apply(consumer_name(kind), environ=first_env)
    assert first.code == 0, first.error
    second_name = "pages-mcp" if kind == "s3" else "url-mcp"
    result = lab.plan(second_name, environ=second_env)
    assert result.code == 0, result.error
    assert not any(item["id"] == "resource_answer_ignored" for item in result.warnings)


def test_other_provider_package_answer_warns(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind != "s3":
        pytest.skip("S3 provider package scenario")
    install_ok_probes(monkeypatch)
    extra = s3_provider("garage")
    lab = Lab(tmp_path, standard_manifests(), extra=[extra])
    first = lab.apply(
        "file-mcp",
        environ={
            "KEYSTONE_SETUP_RESOURCE_S3": "catalog",
            "KEYSTONE_SETUP_RESOURCE_S3_PACKAGE": "local/minio",
        },
    )
    assert first.code == 0, first.error
    result = lab.plan(
        "pages-mcp",
        environ={"KEYSTONE_SETUP_RESOURCE_S3_PACKAGE": "extra/garage"},
    )
    assert result.code in (0, 2), result.error
    matches = [item for item in result.warnings if item["id"] == "resource_answer_ignored"]
    assert len(matches) == 1
    assert "garage" in matches[0]["message"]
    assert "minio" in matches[0]["message"]


def test_one_provider_per_kind(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers() if kind == "s3" else lab.pg_answers()
    bound = lab.apply(consumer_name(kind), environ=answers)
    assert bound.code == 0, bound.error
    result = lab.apply(provider_name(kind))
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "resource_already_bound"
    assert result.error["message"] == (
        f"{kind} is already provided by external; one provider per kind"
    )


def test_catalog_provider_manifest_is_parsed_before_deploy(tmp_path: Path, kind: str) -> None:
    if kind != "s3":
        return
    lab = Lab(tmp_path, standard_manifests())
    document = yaml.safe_load(lab.texts["minio"])
    document["containers"]["server"]["privileged"] = True
    bad = yaml.safe_dump(document)
    lab.by_url["https://example.com/minio.git"] = bad
    seeded = (
        lab.root
        / ".keystonectl"
        / "packages"
        / "local"
        / "minio"
        / "1.0.0"
        / "keystone-service.yaml"
    )
    seeded.write_text(bad, encoding="utf-8")
    result = lab.apply(
        "file-mcp",
        environ={
            "KEYSTONE_SETUP_RESOURCE_S3": "catalog",
            "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_MAX_BYTES": "10",
            "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS": "60",
        },
    )
    assert result.code == 3, result.error
    assert result.error is not None
    assert result.error["id"] == "manifest_invalid"
    assert lab.deployer.events == []
    assert not (lab.root / "packages" / "minio.yml").exists()


def test_direct_provider_install_binds_without_consumers(tmp_path: Path, kind: str) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply(provider_name(kind))
    assert result.code == 0, result.error
    lock = yaml.safe_load((lab.root / "services.lock.yaml").read_text(encoding="utf-8"))
    resource = next(item for item in lock["resources"] if item["kind"] == kind)
    assert resource["origin"] == "catalog"
    assert resource["provider"] == provider_name(kind)
    assert resource["consumers"] == []
    service = next(item for item in lock["services"] if item["name"] == provider_name(kind))
    assert service["role"] == "resource"


def test_rerun_is_unchanged(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers() if kind == "s3" else lab.pg_answers()
    first = lab.apply(consumer_name(kind), environ=answers)
    assert first.code == 0, first.error
    probes = len(calls)
    docker = [call for call in lab.runner.calls if call.args[:1] == ("docker",)]
    again = lab.apply(consumer_name(kind), environ=answers)
    assert again.code == 0, again.error
    assert again.changes == 0
    assert len(calls) == probes
    assert [call for call in lab.runner.calls if call.args[:1] == ("docker",)] == docker


def test_reprovision_when_env_lost(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert first.code == 0, first.error
    runs = [call for call in lab.runner.calls if call.args[:2] == ("docker", "run")]
    env_path = lab.root / "packages" / f"{consumer_name(kind)}.env"
    env_path.unlink()
    again = lab.apply(consumer_name(kind), environ={choice_var(kind): "catalog"})
    assert again.code == 0, again.error
    later = [call for call in lab.runner.calls if call.args[:2] == ("docker", "run")]
    assert len(later) == len(runs) + 1


def test_interface_version_mismatch(tmp_path: Path, kind: str) -> None:
    document = s3_consumer() if kind == "s3" else pg_consumer()
    set_need_version(document, kind, 2)
    lab = Lab(tmp_path, [document], validate_manifests=False)
    result = lab.plan(document["name"])
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "manifest_invalid"
    assert "version" in result.error["message"]


def test_remove_provider_in_use_refused(tmp_path: Path, kind: str) -> None:
    provider = provider_name(kind)
    consumer = (
        {"service": "file-mcp", "bucket": "file-mcp"}
        if kind == "s3"
        else {
            "service": "file-mcp",
            "database": "file_mcp",
            "user": "file_mcp",
        }
    )
    lock = {
        "services": [
            {"name": provider, "role": "resource"},
            {"name": "file-mcp", "role": "service"},
        ],
        "resources": [{"kind": kind, "provider": provider, "consumers": [consumer]}],
    }
    refusal = provider_removal_error(lock, provider)
    assert refusal is not None
    assert refusal.error_id == "resource_in_use"
    assert "file-mcp" in refusal.message
    assert provider_removal_error(lock, "other") is None


def test_unbind_consumer_keeps_data(tmp_path: Path, kind: str) -> None:
    provider = provider_name(kind)
    if kind == "s3":
        consumer = {"service": "file-mcp", "bucket": "file-mcp"}
        expected = "bucket file-mcp in s3 (minio) was kept"
    else:
        consumer = {"service": "file-mcp", "database": "file_mcp", "user": "file_mcp"}
        expected = "database file_mcp in postgres (postgres) was kept"
    lock = {
        "services": [
            {"name": provider, "role": "resource"},
            {"name": "file-mcp", "role": "service", "resources": [kind]},
        ],
        "resources": [
            {
                "kind": kind,
                "provider": provider,
                "consumers": [consumer],
            }
        ],
    }
    assert unbind_consumer(lock, "file-mcp") == [expected]
    assert lock["resources"][0]["consumers"] == []
    assert lock["resources"][0]["kind"] == kind
    assert lock["services"][1]["resources"] == []


def test_lock_has_no_secret_values(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers() if kind == "s3" else lab.pg_answers()
    result = lab.apply(consumer_name(kind), environ=answers)
    assert result.code == 0, result.error
    text = (lab.root / "services.lock.yaml").read_text(encoding="utf-8")
    assert lab.secrets.values
    for value in lab.secrets.values:
        assert value not in text
    env_path = lab.root / "packages" / f"{consumer_name(kind)}.env"
    assert env_path.is_file()
