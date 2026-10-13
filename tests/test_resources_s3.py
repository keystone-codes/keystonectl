"""External s3/v1 binding."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.envfiles import read_env
from keystonectl.resources import RESOURCE_NETWORK
from tests.support import mode
from tests.support.resources import (
    ACCESS_KEY,
    SECRET_KEY,
    Lab,
    install_ok_probes,
    install_scripted_probe,
    s3_report,
    standard_manifests,
)


def test_external_asks_only_required(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.plan("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "external"})
    assert result.code == 2
    variables = {item["var"] for item in result.missing}
    assert "KEYSTONE_SETUP_S3_ENDPOINT" in variables
    assert "KEYSTONE_SETUP_S3_BUCKET" in variables
    assert "KEYSTONE_SETUP_S3_ACCESS_KEY_FILE" in variables
    assert "KEYSTONE_SETUP_S3_SECRET_KEY_FILE" in variables
    assert "KEYSTONE_SETUP_S3_REGION" not in variables
    assert "KEYSTONE_SETUP_S3_PATH_STYLE" not in variables


def test_service_specific_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers(KEYSTONE_SETUP_S3_BUCKET="general-bucket")
    answers["KEYSTONE_SETUP_SERVICE_FILE_MCP_S3_BUCKET"] = "special-bucket"
    result = lab.apply("file-mcp", environ=answers)
    assert result.code == 0, result.error
    found = read_env(lab.root / "packages" / "file-mcp.env")
    assert found["FILE_MCP_S3_BUCKET"] == "special-bucket"
    assert calls[0][1].bucket == "special-bucket"


def test_secret_file_must_be_private(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers()
    Path(answers["KEYSTONE_SETUP_S3_SECRET_KEY_FILE"]).chmod(0o644)
    result = lab.plan("file-mcp", environ=answers)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "secret_file_unsafe"


def test_external_probe_ok_writes_env_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply("file-mcp", environ=lab.s3_answers())
    assert result.code == 0, result.error
    env_path = lab.root / "packages" / "file-mcp.env"
    assert mode(env_path) == 0o600
    found = read_env(env_path)
    assert set(found) >= {
        "FILE_MCP_S3_ENDPOINT",
        "FILE_MCP_S3_REGION",
        "FILE_MCP_S3_BUCKET",
        "FILE_MCP_S3_ACCESS_KEY",
        "FILE_MCP_S3_SECRET_KEY",
        "FILE_MCP_S3_PATH_STYLE",
    }
    assert found["FILE_MCP_S3_SECRET_KEY"] == SECRET_KEY
    assert found["FILE_MCP_S3_ACCESS_KEY"] == ACCESS_KEY
    assert found["FILE_MCP_S3_PATH_STYLE"] == "true"
    lock = yaml.safe_load((lab.root / "services.lock.yaml").read_text(encoding="utf-8"))
    resource = lock["resources"][0]
    assert resource["kind"] == "s3"
    assert resource["origin"] == "external"
    assert resource["provider"] is None
    assert resource["consumers"][0]["bucket"] == "file-mcp"
    assert SECRET_KEY not in (lab.root / "services.lock.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("exit_code", "probe_code", "error_id"),
    [
        (0, None, None),
        (1, "auth_failed", "s3_access_denied"),
        (1, "bucket_not_found", "s3_bucket_missing"),
        (1, "wrong_region", "s3_wrong_region"),
        (1, "presign_put_failed", "s3_presign_failed"),
        (1, "presign_get_failed", "s3_presign_failed"),
        (3, "timeout", "s3_unreachable"),
        (4, "lifecycle_unsupported", None),
        (2, None, None),
    ],
)
def test_probe_result_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    probe_code: str | None,
    error_id: str | None,
) -> None:
    missing = ["bucket"] if exit_code == 2 else None
    install_scripted_probe(
        monkeypatch,
        "s3",
        [s3_report(exit_code=exit_code, code=probe_code, missing=missing)],
    )
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply("file-mcp", environ=lab.s3_answers())
    if exit_code in {0, 4}:
        assert result.code == 0, result.error
        assert "s3" in result.result["probes"]
        if exit_code == 4:
            assert any(item["id"] == "s3_lifecycle_unsupported" for item in result.warnings)
        return
    if exit_code == 2:
        assert result.code == 2
        assert any(item["var"] == "KEYSTONE_SETUP_S3_BUCKET" for item in result.missing)
        return
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == error_id
    assert result.retryable is (error_id == "s3_unreachable")


def test_docker_name_endpoint_checked_via_container_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    lab.containers[RESOURCE_NETWORK] = {
        "abc": {"Name": "minio", "IPv4Address": "10.0.0.8/16", "Aliases": ["minio"]}
    }
    answers = lab.s3_answers(KEYSTONE_SETUP_S3_ENDPOINT="http://minio:9000")
    result = lab.apply("file-mcp", environ=answers)
    assert result.code == 0, result.error
    assert calls[0][2]["connect_endpoint"] == "http://10.0.0.8:9000"
    assert result.result["probes"]["s3"]["checked_via"] == "http://10.0.0.8:9000"


def test_plain_http_warns(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.s3_answers(KEYSTONE_SETUP_S3_ENDPOINT="http://s3.example")
    result = lab.plan("file-mcp", environ=answers)
    assert result.code == 0, result.error
    assert any(item["id"] == "s3_plain_http" for item in result.warnings)
    assert any(item["id"] == "external.probe" for item in result.actions)
