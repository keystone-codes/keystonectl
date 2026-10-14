"""External and catalog postgres/v1 binding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from keystonectl.envfiles import read_env
from keystonectl.resources.postgres_probe import PgParams, build_url
from keystonectl.resources.provision import consumer_secret_path, existing_consumer_secret
from tests.support import mode
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


NOTES_BIND = {
    "host": "NOTES_PG_HOST",
    "port": "NOTES_PG_PORT",
    "database": "NOTES_PG_DATABASE",
    "user": "NOTES_PG_USER",
    "password": "NOTES_PG_PASSWORD",
    "sslmode": "NOTES_PG_SSLMODE",
}


def _capture_provision_secrets(lab: Lab) -> list[str]:
    seen: list[str] = []
    updated = []
    for prefix, outcome in lab.runner.rules:
        if tuple(prefix) != ("docker",):
            updated.append((prefix, outcome))
            continue

        def wrapped(args, _outcome=outcome, **kwargs):
            if tuple(args[:2]) == ("docker", "run"):
                out = next(
                    str(item)[: -len(":/out")] for item in args if str(item).endswith(":/out")
                )
                consumer = list(args)[list(args).index("--consumer") + 1]
                text = (Path(out) / f"{consumer}.secret").read_text(encoding="utf-8")
                assert all(text not in str(arg) for arg in args)
                # provision.sh requires a trailing newline (POSIX read at EOF), but
                # _read_secret() strips it before the secret reaches callers. Compare
                # the same way the production code does.
                seen.append(text.rstrip("\n"))
            return _outcome(args, **kwargs)

        updated.append((prefix, wrapped))
    lab.runner.rules = updated
    return seen


def _postgres_resource(lab: Lab) -> dict:
    document = yaml.safe_load((lab.root / "services.lock.yaml").read_text(encoding="utf-8"))
    return next(item for item in document.get("resources") or [] if item.get("kind") == "postgres")


def test_failed_probe_keeps_consumer_secret_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_scripted_probe(
        monkeypatch,
        "postgres",
        [pg_report(exit_code=1, code="auth_failed"), pg_report()],
    )
    lab = Lab(tmp_path, standard_manifests())
    seen = _capture_provision_secrets(lab)
    failed = lab.apply("notes-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"})
    assert failed.code == 3
    assert failed.error is not None
    assert failed.error["id"] == "postgres_access_denied"
    assert seen
    secret = seen[0]
    env_path = lab.root / "packages" / "notes-mcp.env"
    assert not env_path.exists()
    secret_path = consumer_secret_path(lab.root, "notes-mcp", "postgres")
    assert mode(secret_path) == 0o600
    assert existing_consumer_secret(read_env(secret_path), NOTES_BIND) == secret
    lock_text = (lab.root / "services.lock.yaml").read_text(encoding="utf-8")
    assert secret not in lock_text
    assert secret not in json.dumps(failed.to_dict())
    resources = [
        item
        for item in yaml.safe_load(lock_text).get("resources") or []
        if item.get("kind") == "postgres"
    ]
    assert all(
        "notes-mcp" not in [c.get("service") for c in item.get("consumers") or []]
        for item in resources
    )

    again = lab.apply("notes-mcp", environ={"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"})
    assert again.code == 0, again.error
    assert seen[1] == secret
    assert read_env(env_path)["NOTES_PG_PASSWORD"] == secret
    resource = _postgres_resource(lab)
    assert any(item.get("service") == "notes-mcp" for item in resource["consumers"])
    saved = (lab.root / "services.lock.yaml").read_text(encoding="utf-8")
    assert secret not in saved
    assert secret not in json.dumps(again.to_dict())


def test_second_url_cannot_retarget_shared_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply("notes-mcp", environ=lab.pg_answers())
    assert first.code == 0, first.error
    original = _postgres_resource(lab)["params"]
    assert original == {"host": "db.example", "port": 5432, "sslmode": "require"}

    other_password = "other-password-value-0123456789ab"
    bad = build_url(
        PgParams(
            host="other.example",
            port=5433,
            database="otherdb",
            user="other",
            password=other_password,
            sslmode="disable",
        )
    )
    refused = lab.apply(
        "url-mcp",
        environ={"KEYSTONE_SETUP_POSTGRES_URL_FILE": lab.url_file(bad)},
    )
    assert refused.code == 3
    assert refused.error is not None
    assert refused.error["id"] == "postgres_address_mismatch"
    assert other_password not in refused.error["message"]
    assert other_password not in json.dumps(refused.to_dict())
    kept = _postgres_resource(lab)
    assert kept["params"] == original
    assert [item["service"] for item in kept["consumers"]] == ["notes-mcp"]
    assert not (lab.root / "packages" / "url-mcp.env").exists()

    good_password = "good-password-value-0123456789abcd"
    good = build_url(
        PgParams(
            host="db.example",
            port=5432,
            database="librarydb",
            user="library",
            password=good_password,
            sslmode="require",
        )
    )
    accepted = lab.apply(
        "url-mcp",
        environ={"KEYSTONE_SETUP_POSTGRES_URL_FILE": lab.url_file(good)},
    )
    assert accepted.code == 0, accepted.error
    resource = _postgres_resource(lab)
    assert resource["params"] == original
    consumer = next(item for item in resource["consumers"] if item["service"] == "url-mcp")
    assert consumer["database"] == "librarydb"
    assert consumer["user"] == "library"
    assert "password" not in consumer
    lock_text = (lab.root / "services.lock.yaml").read_text(encoding="utf-8")
    assert other_password not in lock_text
    assert good_password not in lock_text
    assert PG_PASSWORD not in lock_text
    assert good_password not in json.dumps(accepted.to_dict())


def test_sslmode_disable_warns_for_external(tmp_path: Path) -> None:
    lab = Lab(tmp_path, standard_manifests())
    answers = lab.pg_answers(KEYSTONE_SETUP_POSTGRES_SSLMODE="disable")
    result = lab.plan("notes-mcp", environ=answers)
    assert result.code == 0, result.error
    assert any(item["id"] == "postgres_plain_connection" for item in result.warnings)
    assert any(item["id"] == "external.probe" for item in result.actions)
    assert not any(item["var"].startswith("KEYSTONE_SETUP_POSTGRES") for item in result.missing)
