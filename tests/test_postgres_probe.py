"""Unit tests for postgres/v1 probe."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import psycopg
import pytest

from keystonectl.resources import postgres_probe as probe
from tests.support.fake_pg import (
    FakePgConnection,
    auth_error_sqlstate,
    make_connect_factory,
    ok_responses,
    permission_denied,
)

PROBE_ID = "abc123"
GOOD = probe.PgParams(
    host="postgres-db",
    port=5432,
    database="docs_service",
    user="docs_service",
    password="s3cr3t",
    sslmode="disable",
)


def _run(params: probe.PgParams | None = None, **kwargs):
    params = params or GOOD
    factory = make_connect_factory(ok_responses())
    report = probe.probe_postgres(
        params,
        connect=factory,
        probe_id=PROBE_ID,
        **kwargs,
    )
    return report, factory


def test_all_checks_ok_exit_0():
    report, _ = _run()
    assert report.exit_code == 0
    assert report.ok is True
    assert report.checks["isolation"] == "ok"


def test_probe_never_toggles_autocommit():
    # psycopg 3 raises if autocommit is assigned while a transaction is already open
    # (INTRANS), which is the case here right after the version checks run. The probe's
    # create-table/rollback dance must rely on the connection's default transaction mode
    # instead of touching `.autocommit`.
    created: list[FakePgConnection] = []
    responses = ok_responses()

    def factory(**kwargs):
        conn = FakePgConnection(responses=responses, connect_kwargs=dict(kwargs))
        created.append(conn)
        return conn

    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 0, report.error
    assert len(created) == 1
    assert created[0].autocommit_assignments == []


def test_missing_parameters_exit_2():
    params = probe.PgParams(
        host="postgres-db",
        port=5432,
        database="",
        user="docs_service",
        password="",
        sslmode="disable",
    )
    report = probe.probe_postgres(params, connect=make_connect_factory({}), probe_id=PROBE_ID)
    assert report.exit_code == 2
    assert report.error.code == "missing_parameters"
    assert sorted(report.missing) == ["database", "password"]


def test_invalid_sslmode_and_database_name_exit_2():
    params = probe.PgParams(
        host="postgres-db",
        port=5432,
        database="Bad-Name",
        user="docs_service",
        password="s3cr3t",
        sslmode="nope",
    )
    report = probe.probe_postgres(params, connect=make_connect_factory({}), probe_id=PROBE_ID)
    assert report.exit_code == 2
    assert report.error.code == "invalid_parameters"


def test_params_from_url_and_parts_agree():
    params = probe.PgParams(
        host="db.example.com",
        port=5433,
        database="docs_service",
        user="docs_service",
        password="p@ss:w/rd",
        sslmode="require",
    )
    roundtrip = probe.params_from_url(probe.build_url(params))
    assert roundtrip == params


def test_params_from_env_url_wins(monkeypatch):
    monkeypatch.setenv("KEYSTONE_POSTGRES_URL", probe.build_url(GOOD))
    monkeypatch.setenv("KEYSTONE_POSTGRES_HOST", "wrong")
    monkeypatch.setenv("KEYSTONE_POSTGRES_DATABASE", "wrong")
    got = probe.params_from_env(os.environ)
    assert got == GOOD


def test_params_file_readable_by_others_refused(tmp_path: Path):
    path = tmp_path / "probe.yaml"
    path.write_text("host: h\nport: 5432\ndatabase: d\nuser: u\npassword: p\nsslmode: disable\n")
    path.chmod(stat.S_IRUSR | stat.S_IRGRP)
    with pytest.raises(probe.ParamsError, match="chmod 600"):
        probe.params_from_file(path)


def test_auth_failed_by_sqlstate_exit_1():
    factory = make_connect_factory({}, connect_error=auth_error_sqlstate())
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "auth_failed"


def test_auth_failed_by_message_exit_1():
    factory = make_connect_factory(
        {},
        connect_error=psycopg.OperationalError("password authentication failed for user"),
    )
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "auth_failed"


def test_database_not_found_exit_1():
    err = psycopg.errors.lookup("3D000")('database "missing_db" does not exist')
    factory = make_connect_factory({}, connect_error=err)
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "database_not_found"


def test_endpoint_unreachable_exit_3():
    factory = make_connect_factory(
        {},
        connect_error=psycopg.OperationalError("connection refused"),
    )
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 3
    assert report.error.code == "endpoint_unreachable"
    assert report.error.retryable is True


def test_version_too_old_exit_1():
    factory = make_connect_factory(ok_responses(server_version_num=130012))
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "version_too_old"


def test_create_table_permission_denied_exit_1():
    responses = ok_responses()
    responses["create table"] = [permission_denied()]
    factory = make_connect_factory(responses)
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "create_table_failed"


def test_probe_table_always_rolled_back():
    responses = ok_responses()
    last_conn: list = []

    def tracking_factory(**kwargs):
        conn = make_connect_factory(responses)(**kwargs)
        last_conn.clear()
        last_conn.append(conn)
        return conn

    report = probe.probe_postgres(GOOD, connect=tracking_factory, probe_id=PROBE_ID)
    assert report.exit_code == 0
    assert last_conn[0].rollback_calls >= 1
    assert last_conn[0].commit_calls == 0

    responses_fail = ok_responses()
    responses_fail["create table"] = [permission_denied()]
    last_conn.clear()

    def failing_factory(**kwargs):
        conn = make_connect_factory(responses_fail)(**kwargs)
        last_conn.append(conn)
        return conn

    report = probe.probe_postgres(GOOD, connect=failing_factory, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert last_conn[0].rollback_calls >= 1
    assert last_conn[0].commit_calls == 0


def test_extension_missing_exit_1():
    factory = make_connect_factory(ok_responses(extensions=["pgcrypto"]))
    report = probe.probe_postgres(
        GOOD,
        extensions=["pgcrypto", "vector"],
        connect=factory,
        probe_id=PROBE_ID,
    )
    assert report.exit_code == 1
    assert report.error.code == "extension_missing"
    assert report.error.missing_extensions == ["vector"]


def test_extensions_skipped_when_not_requested():
    report, _ = _run(extensions=[])
    assert report.checks["extensions"] == "skipped"


def test_isolation_warnings_without_flag():
    factory = make_connect_factory(
        ok_responses(rolsuper=True, other_databases=["other_db"]),
    )
    report = probe.probe_postgres(GOOD, connect=factory, probe_id=PROBE_ID)
    assert report.exit_code == 0
    assert report.checks["isolation"] == "weak"
    assert "superuser_credentials" in report.warnings
    assert "connect_to_other_databases" in report.warnings


def test_require_isolation_turns_warnings_into_error():
    factory = make_connect_factory(ok_responses(rolsuper=True))
    report = probe.probe_postgres(
        GOOD,
        connect=factory,
        require_isolation=True,
        probe_id=PROBE_ID,
    )
    assert report.exit_code == 1
    assert report.error.code == "not_isolated"


def test_connect_override_used():
    factory = make_connect_factory(ok_responses())
    report = probe.probe_postgres(
        GOOD,
        connect=factory,
        connect_host="127.0.0.1",
        connect_port=55432,
        probe_id=PROBE_ID,
    )
    assert report.host == GOOD.host
    assert report.checked_via == "127.0.0.1:55432"
    kwargs = factory.captured["kwargs"]  # type: ignore[attr-defined]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 55432


def test_insecure_sslmode_warning():
    params = probe.PgParams(
        host="db.example.com",
        port=5432,
        database="docs_service",
        user="docs_service",
        password="s3cr3t",
        sslmode="disable",
    )
    report, _ = _run(params)
    assert "insecure_sslmode" in report.warnings

    report2, _ = _run(GOOD)
    assert "insecure_sslmode" not in report2.warnings


def test_secrets_never_printed(capsys, caplog, monkeypatch):
    password = "super-secret-value-xyz"
    err = psycopg.OperationalError(f"FATAL: password authentication failed for user ({password})")
    factory = make_connect_factory({}, connect_error=err)
    monkeypatch.setenv("KEYSTONE_POSTGRES_HOST", GOOD.host)
    monkeypatch.setenv("KEYSTONE_POSTGRES_PORT", str(GOOD.port))
    monkeypatch.setenv("KEYSTONE_POSTGRES_DATABASE", GOOD.database)
    monkeypatch.setenv("KEYSTONE_POSTGRES_USER", GOOD.user)
    monkeypatch.setenv("KEYSTONE_POSTGRES_PASSWORD", password)
    monkeypatch.setenv("KEYSTONE_POSTGRES_SSLMODE", GOOD.sslmode)
    monkeypatch.setattr(probe, "_default_connect", factory)
    caplog.set_level(logging.DEBUG)

    code = probe.main(["--json"])

    assert code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert password not in combined
    assert "postgresql://" not in combined
    assert "***" in combined


def test_cli_json_shape(capsys, monkeypatch):
    factory = make_connect_factory(ok_responses())
    monkeypatch.setenv("KEYSTONE_POSTGRES_HOST", GOOD.host)
    monkeypatch.setenv("KEYSTONE_POSTGRES_PORT", str(GOOD.port))
    monkeypatch.setenv("KEYSTONE_POSTGRES_DATABASE", GOOD.database)
    monkeypatch.setenv("KEYSTONE_POSTGRES_USER", GOOD.user)
    monkeypatch.setenv("KEYSTONE_POSTGRES_PASSWORD", GOOD.password)
    monkeypatch.setenv("KEYSTONE_POSTGRES_SSLMODE", GOOD.sslmode)
    monkeypatch.setattr(probe, "_default_connect", factory)
    code = probe.main(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(data) == {
        "ok",
        "exit_code",
        "host",
        "port",
        "database",
        "user",
        "sslmode",
        "server_version",
        "checks",
        "warnings",
        "error",
        "missing",
    }
