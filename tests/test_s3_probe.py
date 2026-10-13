"""Tests for S3/v1 probe (no live network)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from keystonectl.resources import s3_probe

PROBE_ID = "fixed-probe-id"
VALID_PARAMS = s3_probe.S3Params(
    endpoint="http://minio-server:9000",
    region="us-east-1",
    bucket="probe-bucket",
    access_key="test-access-key",
    secret_key="test-secret",
    path_style=True,
)


def _client_and_stubber(params: s3_probe.S3Params | None = None):
    params = params or VALID_PARAMS
    client = s3_probe.make_client(params)
    stubber = Stubber(client)
    return client, stubber


def _success_stub_sequence(stubber: Stubber, *, lifecycle_rules: list | None = None) -> bytes:
    body = f"keystone s3 probe {PROBE_ID}\n".encode()
    key = f"{s3_probe.PROBE_PREFIX}{PROBE_ID}.txt"

    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})

    if lifecycle_rules is None:
        lifecycle_rules = []
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {"Rules": lifecycle_rules},
        {"Bucket": VALID_PARAMS.bucket},
    )
    expected_rules = s3_probe._merge_lifecycle_rules(lifecycle_rules)
    stubber.add_response(
        "put_bucket_lifecycle_configuration",
        {},
        {"Bucket": VALID_PARAMS.bucket, "LifecycleConfiguration": {"Rules": expected_rules}},
    )
    stubber.add_response("delete_object", {}, {"Bucket": VALID_PARAMS.bucket, "Key": key})
    return body


def _mock_transport(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=body)
        return httpx.Response(405)

    return httpx.MockTransport(handler)


def test_all_capabilities_ok_exit_0():
    client, stubber = _client_and_stubber()
    body = _success_stub_sequence(stubber)
    transport = _mock_transport(body)
    http = httpx.Client(transport=transport)

    with stubber:
        report = s3_probe.probe_s3(
            VALID_PARAMS,
            client=client,
            http=http,
            probe_id=PROBE_ID,
        )

    assert report.exit_code == 0
    assert report.ok is True
    assert report.checks == {
        "head_bucket": "ok",
        "presign_put": "ok",
        "presign_get": "ok",
        "lifecycle": "ok",
        "cleanup": "ok",
    }


def test_missing_parameters_exit_2():
    params = s3_probe.S3Params(
        endpoint="http://minio:9000",
        region="us-east-1",
        bucket="",
        access_key="key",
        secret_key="",
        path_style=True,
    )
    report = s3_probe.probe_s3(params)
    assert report.exit_code == 2
    assert report.error.code == "missing_parameters"
    assert report.missing == ["bucket", "secret_key"]


def test_invalid_bucket_name_exit_2():
    params = s3_probe.S3Params(
        endpoint="http://minio:9000",
        region="us-east-1",
        bucket="My.Bucket",
        access_key="key",
        secret_key="secret",
        path_style=True,
    )
    report = s3_probe.probe_s3(params)
    assert report.exit_code == 2
    assert report.error.code == "invalid_parameters"


def test_params_from_env():
    env = {
        "KEYSTONE_S3_ENDPOINT": "http://minio:9000",
        "KEYSTONE_S3_REGION": "us-east-1",
        "KEYSTONE_S3_BUCKET": "my-bucket",
        "KEYSTONE_S3_ACCESS_KEY": "ak",
        "KEYSTONE_S3_SECRET_KEY": "sk",
        "KEYSTONE_S3_PATH_STYLE": "false",
    }
    params = s3_probe.params_from_env(env)
    assert params.path_style is False
    assert params.bucket == "my-bucket"


def test_params_file_readable_by_others_refused(tmp_path: Path):
    path = tmp_path / "params.yaml"
    path.write_text(
        "endpoint: http://minio:9000\nregion: us-east-1\nbucket: b\n"
        "access_key: a\nsecret_key: s\npath_style: true\n",
        encoding="utf-8",
    )
    path.chmod(0o644)
    with pytest.raises(s3_probe.ParamsError, match="chmod 600"):
        s3_probe.params_from_file(path)


def test_head_bucket_access_denied_exit_1():
    client, stubber = _client_and_stubber()
    stubber.add_client_error(
        "head_bucket",
        service_error_code="AccessDenied",
        http_status_code=403,
    )
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=httpx.Client())
    assert report.exit_code == 1
    assert report.error.code == "auth_failed"


def test_head_bucket_not_found_exit_1():
    client, stubber = _client_and_stubber()
    stubber.add_client_error(
        "head_bucket",
        service_error_code="NoSuchBucket",
        http_status_code=404,
    )
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=httpx.Client())
    assert report.exit_code == 1
    assert report.error.code == "bucket_not_found"


def test_wrong_region_exit_1():
    client, stubber = _client_and_stubber()
    stubber.add_client_error(
        "head_bucket",
        service_error_code="PermanentRedirect",
        http_status_code=301,
    )
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=httpx.Client())
    assert report.exit_code == 1
    assert report.error.code == "wrong_region"


def test_endpoint_unreachable_exit_3():
    client, stubber = _client_and_stubber()

    def _raise_connection(*_args, **_kwargs):
        raise EndpointConnectionError(endpoint_url=VALID_PARAMS.endpoint)

    client.head_bucket = _raise_connection  # type: ignore[method-assign]
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=httpx.Client())
    assert report.exit_code == 3
    assert report.error.code == "endpoint_unreachable"
    assert report.error.retryable is True


def test_provider_5xx_exit_3():
    client, stubber = _client_and_stubber()
    stubber.add_client_error(
        "head_bucket",
        service_error_code="InternalError",
        http_status_code=503,
    )
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=httpx.Client())
    assert report.exit_code == 3
    assert report.error.code == "provider_error"
    assert report.error.retryable is True


def test_presigned_put_rejected_exit_1():
    client, stubber = _client_and_stubber()
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(
                403,
                text='<?xml version="1.0"?><Error><Code>SignatureDoesNotMatch</Code></Error>',
            )
        return httpx.Response(405)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "presign_put_failed"


def test_presigned_get_body_mismatch_exit_1():
    client, stubber = _client_and_stubber()
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=b"wrong")
        return httpx.Response(405)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 1
    assert report.error.code == "presign_get_mismatch"


def test_lifecycle_unsupported_exit_4():
    client, stubber = _client_and_stubber()
    body = f"keystone s3 probe {PROBE_ID}\n".encode()
    key = f"{s3_probe.PROBE_PREFIX}{PROBE_ID}.txt"
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {"Rules": []},
        {"Bucket": VALID_PARAMS.bucket},
    )
    stubber.add_client_error(
        "put_bucket_lifecycle_configuration",
        service_error_code="NotImplemented",
        http_status_code=501,
    )
    stubber.add_response("delete_object", {}, {"Bucket": VALID_PARAMS.bucket, "Key": key})
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 4
    assert report.error.code == "lifecycle_unsupported"
    assert report.checks["lifecycle"] == "unsupported"


def test_lifecycle_keeps_foreign_rules():
    client, stubber = _client_and_stubber()
    foreign = {
        "ID": "other-rule",
        "Filter": {"Prefix": "uploads/"},
        "Status": "Enabled",
        "Expiration": {"Days": 7},
    }
    merged = s3_probe._merge_lifecycle_rules([foreign])
    body = f"keystone s3 probe {PROBE_ID}\n".encode()
    key = f"{s3_probe.PROBE_PREFIX}{PROBE_ID}.txt"
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {"Rules": [foreign]},
        {"Bucket": VALID_PARAMS.bucket},
    )
    stubber.add_response(
        "put_bucket_lifecycle_configuration",
        {},
        {
            "Bucket": VALID_PARAMS.bucket,
            "LifecycleConfiguration": {"Rules": merged},
        },
    )
    stubber.add_response("delete_object", {}, {"Bucket": VALID_PARAMS.bucket, "Key": key})
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 0


def test_lifecycle_replaces_previous_probe_rule():
    client, stubber = _client_and_stubber()
    old_probe = {
        "ID": s3_probe.PROBE_RULE_ID,
        "Filter": {"Prefix": s3_probe.PROBE_PREFIX},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
    }
    merged = s3_probe._merge_lifecycle_rules([old_probe])
    assert sum(1 for r in merged if r.get("ID") == s3_probe.PROBE_RULE_ID) == 1
    body = _success_stub_sequence(stubber, lifecycle_rules=[old_probe])
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 0


def test_skip_lifecycle():
    client, stubber = _client_and_stubber()
    body = f"keystone s3 probe {PROBE_ID}\n".encode()
    key = f"{s3_probe.PROBE_PREFIX}{PROBE_ID}.txt"
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})
    stubber.add_response("delete_object", {}, {"Bucket": VALID_PARAMS.bucket, "Key": key})
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(
            VALID_PARAMS,
            client=client,
            http=http,
            probe_id=PROBE_ID,
            check_lifecycle=False,
        )
    assert report.exit_code == 0
    assert report.checks["lifecycle"] == "skipped"


def test_cleanup_failure_is_warning():
    client, stubber = _client_and_stubber()
    body = f"keystone s3 probe {PROBE_ID}\n".encode()
    stubber.add_response("head_bucket", {}, {"Bucket": VALID_PARAMS.bucket})
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {"Rules": []},
        {"Bucket": VALID_PARAMS.bucket},
    )
    merged = s3_probe._merge_lifecycle_rules([])
    stubber.add_response(
        "put_bucket_lifecycle_configuration",
        {},
        {"Bucket": VALID_PARAMS.bucket, "LifecycleConfiguration": {"Rules": merged}},
    )
    stubber.add_client_error(
        "delete_object",
        service_error_code="AccessDenied",
        http_status_code=403,
    )
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    assert report.exit_code == 0
    assert "cleanup_failed" in report.warnings
    assert report.checks["cleanup"] == "failed"


def test_path_style_addressing():
    params = VALID_PARAMS
    client = s3_probe.make_client(params)
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": params.bucket, "Key": "k"},
        ExpiresIn=300,
    )
    assert "minio-server" in url
    assert f"/{params.bucket}/" in url

    params_virtual = s3_probe.S3Params(
        endpoint="https://s3.amazonaws.com",
        region="us-east-1",
        bucket="my-bucket",
        access_key="ak",
        secret_key="sk",
        path_style=False,
    )
    client_v = s3_probe.make_client(params_virtual)
    url_v = client_v.generate_presigned_url(
        "put_object",
        Params={"Bucket": params_virtual.bucket, "Key": "k"},
        ExpiresIn=300,
    )
    assert url_v.startswith("https://my-bucket.")


def test_secrets_never_printed(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    env = {
        "KEYSTONE_S3_ENDPOINT": "http://minio:9000",
        "KEYSTONE_S3_REGION": "us-east-1",
        "KEYSTONE_S3_BUCKET": "b",
        "KEYSTONE_S3_ACCESS_KEY": "super-access-key-value",
        "KEYSTONE_S3_SECRET_KEY": "test-secret",
        "KEYSTONE_S3_PATH_STYLE": "true",
    }
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(s3_probe, "probe_s3") as mock_probe,
    ):
        mock_probe.return_value = s3_probe.ProbeReport(
            ok=False,
            exit_code=2,
            endpoint=env["KEYSTONE_S3_ENDPOINT"],
            bucket="b",
            region="us-east-1",
            path_style=True,
            checks={},
            warnings=[],
            error=s3_probe.ProbeError(code="missing_parameters", message="missing"),
            missing=["x"],
        )
        s3_probe.main(["--json"])
    out = capsys.readouterr().out
    assert "test-secret" not in out
    assert "super-access-key-value" not in out
    assert "X-Amz-Signature" not in out
    combined_logs = caplog.text + out
    assert "test-secret" not in combined_logs
    assert "super-access-key-value" not in combined_logs


def test_insecure_endpoint_warning():
    params = s3_probe.S3Params(
        endpoint="http://s3.example.com",
        region="us-east-1",
        bucket="my-bucket",
        access_key="ak",
        secret_key="sk",
        path_style=False,
    )
    client, stubber = _client_and_stubber(params)
    body = _success_stub_sequence(stubber)
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(params, client=client, http=http, probe_id=PROBE_ID)
    assert "insecure_endpoint" in report.warnings

    client2, stubber2 = _client_and_stubber(VALID_PARAMS)
    body2 = _success_stub_sequence(stubber2)
    http2 = httpx.Client(transport=_mock_transport(body2))
    with stubber2:
        report2 = s3_probe.probe_s3(VALID_PARAMS, client=client2, http=http2, probe_id=PROBE_ID)
    assert "insecure_endpoint" not in report2.warnings


def test_cli_json_shape():
    client, stubber = _client_and_stubber()
    body = _success_stub_sequence(stubber)
    http = httpx.Client(transport=_mock_transport(body))
    with stubber:
        report = s3_probe.probe_s3(VALID_PARAMS, client=client, http=http, probe_id=PROBE_ID)
    data = report.to_json_dict()
    expected_keys = {
        "ok",
        "exit_code",
        "endpoint",
        "bucket",
        "region",
        "path_style",
        "checks",
        "warnings",
        "error",
        "missing",
    }
    assert set(data.keys()) == expected_keys


def test_endpoint_override_used_for_requests_and_links():
    override = "http://127.0.0.1:9000"
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=f"keystone s3 probe {PROBE_ID}\n".encode())
        return httpx.Response(405)

    client = s3_probe.make_client(VALID_PARAMS, endpoint_url=override)
    stubber = Stubber(client)
    _success_stub_sequence(stubber)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with stubber:
        report = s3_probe.probe_s3(
            VALID_PARAMS,
            client=client,
            http=http,
            probe_id=PROBE_ID,
            connect_endpoint=override,
        )
    assert report.endpoint == VALID_PARAMS.endpoint
    assert report.checked_via == override
    assert seen_hosts
    assert all(h == "127.0.0.1" for h in seen_hosts)
    put_url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": VALID_PARAMS.bucket, "Key": "k"},
        ExpiresIn=300,
    )
    rewritten = s3_probe._rewrite_presigned_host(put_url, override)
    assert rewritten.startswith("http://127.0.0.1:9000")
