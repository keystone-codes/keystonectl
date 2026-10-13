"""External and catalog postgres/v1 binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from keystonectl.envfiles import read_env
from keystonectl.resources.postgres_probe import PgParams, build_url
from tests.support.resources import (
    PG_PASSWORD,
    Lab,
    install_ok_probes,
    install_scripted_probe,
    pg_consumer,
    pg_provider,
    pg_report,
    standard_manifests,
)


def test_external_asks_fields(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    result = lab.plan("notes-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "external"})
    assert result.code == 2
    variables = {item["var"] for item in result.missing}
    assert "KEYSTONE_SETUP_POSTGRES_HOST" in variables
    assert "KEYSTONE_SETUP_POSTGRES_DATABASE" in variables
    assert "KEYSTONE_SETUP_POSTGRES_USER" in variables
    assert "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE" in variables
    assert "KEYSTONE_SETUP_POSTGRES_PORT" not in variables
    assert "KEYSTONE_SETUP_POSTGRES_SSLMODE" not in variables


def test_url_file_replaces_fields(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    url = build_url(
        PgParams(
            host="db.example",
            port=5432,
            database="notesdb",
            user="notes",
            password=PG_PASSWORD,
            sslmode="require",
        )
    )
    result = lab.plan(
        "notes-mcp",
        environ={
            "KEYSTONE_SETUP_RESOURCE_POSTGRES": "external",
            "KEYSTONE_SETUP_POSTGRES_URL_FILE": lab.url_file(url),
        },
    )
    assert result.code == 0, result.error
    variables = {item["var"] for item in result.missing}
    assert "KEYSTONE_SETUP_POSTGRES_HOST" not in variables
    assert "KEYSTONE_SETUP_POSTGRES_DATABASE" not in variables
    assert "KEYSTONE_SETUP_POSTGRES_USER" not in variables
    assert "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE" not in variables
    assert any(item["id"] == "external.probe" for item in result.actions)


def test_url_file_without_sslmode_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    url = f"postgresql://notes:{PG_PASSWORD}@db.example:5432/notesdb"
    result = lab.plan(
        "notes-mcp",
        environ={
            "KEYSTONE_SETUP_RESOURCE_POSTGRES": "external",
            "KEYSTONE_SETUP_POSTGRES_URL_FILE": lab.url_file(url),
        },
    )
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "postgres_url_invalid"


def test_bind_env_url_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_ok_probes(monkeypatch)
    password = "p@ss/w%rd"
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.pg_answers(password=password)
    answers["KEYSTONE_SETUP_POSTGRES_DATABASE"] = "notesdb"
    answers["KEYSTONE_SETUP_POSTGRES_USER"] = "notes"
    result = lab.apply("url-mcp", environ=answers)
    assert result.code == 0, result.error
    found = read_env(lab.root / "packages" / "url-mcp.env")
    expected = build_url(
        PgParams(
            host="db.example",
            port=5432,
            database="notesdb",
            user="notes",
            password=password,
            sslmode="require",
        )
    )
    assert found == {"APP_DATABASE_URL": expected}
    assert "p%40ss%2Fw%25rd" in expected
    assert password not in expected


def test_bind_env_fields_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply("notes-mcp", environ=lab.pg_answers())
    assert result.code == 0, result.error
    found = read_env(lab.root / "packages" / "notes-mcp.env")
    assert found["NOTES_PG_HOST"] == "db.example"
    assert found["NOTES_PG_PORT"] == "5432"
    assert found["NOTES_PG_DATABASE"] == "notesdb"
    assert found["NOTES_PG_USER"] == "notes"
    assert found["NOTES_PG_PASSWORD"] == PG_PASSWORD
    assert found["NOTES_PG_SSLMODE"] == "require"
    assert "APP_DATABASE_URL" not in found


def test_consumer_db_name_dash_to_underscore(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [pg_consumer("file-mcp", namespace="files"), pg_provider()])
    result = lab.plan("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"})
    assert result.code == 0, result.error
    titles = [item["title"] for item in result.actions]
    assert any(
        "Create database file_mcp and user file_mcp in postgres" in title for title in titles
    )


def test_extensions_passed_to_provision_and_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply("notes-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"})
    assert result.code == 0, result.error
    run = next(call for call in lab.runner.calls if call.args[:2] == ("docker", "run"))
    args = list(run.args)
    assert args[args.index("--extensions") + 1] == "pgcrypto"
    postgres_calls = [item for item in calls if item[0] == "postgres"]
    assert postgres_calls[0][2]["extensions"] == ("pgcrypto",)


def test_probe_result_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        (1, "auth_failed", "postgres_access_denied", False, None),
        (1, "database_not_found", "postgres_database_missing", False, None),
        (1, "version_too_old", "postgres_version_too_old", False, None),
        (1, "create_table_failed", "postgres_create_table_failed", False, None),
        (1, "not_isolated", "postgres_not_isolated", False, None),
        (1, "extension_missing", "postgres_extension_missing", False, ["pgcrypto"]),
        (3, "timeout", "postgres_unreachable", True, None),
    ]
    for exit_code, probe_code, error_id, retryable, missing_extensions in cases:
        report = pg_report(
            exit_code=exit_code,
            code=probe_code,
            missing_extensions=missing_extensions,
            message="extension pgcrypto is missing",
        )
        install_scripted_probe(monkeypatch, "postgres", [report])
        lab = Lab(tmp_path, standard_manifests())
        result = lab.apply("notes-mcp", environ=lab.pg_answers())
        assert result.code == 3, probe_code
        assert result.error is not None
        assert result.error["id"] == error_id
        assert result.retryable is retryable
        if probe_code == "extension_missing":
            assert "pgcrypto" in result.error["message"]


def test_catalog_provider_probed_with_require_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    result = lab.apply("notes-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"})
    assert result.code == 0, result.error
    postgres_calls = [item for item in calls if item[0] == "postgres"]
    assert postgres_calls[0][2]["require_isolation"] is True


def test_sslmode_disable_warns_for_external(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.pg_answers(KEYSTONE_SETUP_POSTGRES_SSLMODE="disable")
    result = lab.plan("notes-mcp", environ=answers)
    assert result.code == 0, result.error
    assert any(item["id"] == "postgres_plain_connection" for item in result.warnings)
    assert any(item["id"] == "external.probe" for item in result.actions)
    assert not any(item["var"].startswith("KEYSTONE_SETUP_POSTGRES") for item in result.missing)
