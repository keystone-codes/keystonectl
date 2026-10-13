"""Fixed queries over keystonectl JSON."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "e2e" / "jsonq.py"
_SPEC = importlib.util.spec_from_file_location("e2e_jsonq", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
jsonq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jsonq)


def _doc() -> dict:
    return {
        "code": 2,
        "changes": 0,
        "retryable": False,
        "missing": [
            {
                "var": "KEYSTONE_SETUP_RESOURCE_S3",
                "kind": "choice",
                "choices": ["external", "catalog"],
            },
            {"var": "KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE", "kind": "confirm"},
        ],
        "warnings": [{"id": "local_source", "message": "local"}],
        "error": {"id": "fragment_modified", "message": "edited"},
        "result": {
            "kept": ["bucket file-mcp in s3 (minio) was kept", "packages/file-mcp.override.yml"],
            "compose_command": "docker compose -f docker-compose.yml -f docker-compose.packages.yml up -d",
            "public": {"state": "pending", "tls": "manual"},
            "packages": [{"name": "file-mcp", "status": "unchanged"}],
            "recommended_policy": {"content": "roles:"},
            "services": [
                {
                    "name": "file-mcp",
                    "managed": "generated",
                    "containers": [
                        {"role": "mcp", "state": "running", "health": "healthy", "exit_code": 0}
                    ],
                    "problems": [],
                    "warnings": [{"id": "tls_untrusted", "message": "untrusted"}],
                    "keystone": {"ready": "ready", "discovery": "available"},
                    "public": {"state": "ready", "tls": "custom", "inside": "ok"},
                }
            ],
            "checks": [
                {"id": "compose_version", "status": "ok"},
                {"id": "layout_invalid", "status": "error"},
            ],
        },
    }


def test_queries_are_one_value_per_line() -> None:
    document = _doc()
    assert jsonq.query_lines(document, "code", None) == ["2"]
    assert jsonq.query_lines(document, "missing-vars", None) == [
        "KEYSTONE_SETUP_RESOURCE_S3",
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE",
    ]
    assert jsonq.query_lines(document, "confirm-vars", None) == [
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE"
    ]
    assert jsonq.query_lines(document, "missing-kind", None) == ["choice", "confirm"]
    assert jsonq.query_lines(document, "missing-choices", None) == ["external", "catalog"]
    assert jsonq.query_lines(document, "error-id", None) == ["fragment_modified"]
    assert jsonq.query_lines(document, "retryable", None) == ["false"]
    assert jsonq.query_lines(document, "changes", None) == ["0"]
    assert jsonq.query_lines(document, "kept", None)[0].startswith("bucket ")
    assert jsonq.query_lines(document, "warning-ids", None) == ["local_source", "tls_untrusted"]
    assert jsonq.query_lines(document, "problems", None) == ["layout_invalid"]
    assert "docker-compose.packages.yml" in jsonq.query_lines(document, "compose-command", None)[0]
    assert jsonq.query_lines(document, "public-state", None) == ["pending"]
    assert jsonq.query_lines(document, "public-tls", None) == ["manual"]
    assert jsonq.query_lines(document, "packages", None) == ["file-mcp unchanged"]
    assert jsonq.query_lines(document, "has-recommended-policy", None) == ["yes"]
    status = jsonq.query_lines(document, "package-status", "file-mcp")
    assert "managed generated" in status
    assert "warning tls_untrusted" in status
    assert "keystone ready" in status
    assert jsonq.query_lines(document, "package-status", "absent") == ["missing"]


def test_unknown_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown query"):
        jsonq.query_lines({}, "eval", None)


def test_main_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"code": 0, "retryable": True})))
    assert jsonq.main(["code"]) == 0
    assert capsys.readouterr().out == "0\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    assert jsonq.main(["code"]) == 2
