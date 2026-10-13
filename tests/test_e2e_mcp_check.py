"""The e2e MCP client reports steps without secrets or token URLs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "e2e" / "mcp_check.py"
_SPEC = importlib.util.spec_from_file_location("e2e_mcp_check", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
mcp_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp_check)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.data = None


def test_host_path_drops_the_token() -> None:
    shown = mcp_check.host_path("https://files.e2e.test/u/secret-token?sig=abc#x")
    assert shown == "files.e2e.test/u/secret-token"
    assert "sig" not in shown
    assert "?" not in shown


def test_mcp_endpoint_adds_the_mcp_path() -> None:
    assert mcp_check.mcp_endpoint("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/mcp/"
    assert mcp_check.mcp_endpoint("http://127.0.0.1:8080/mcp") == "http://127.0.0.1:8080/mcp/"


def test_files_round_trip_hides_urls() -> None:
    uploads: dict[str, bytes] = {}

    def call(name: str, arguments: dict) -> _Result:
        if name == "files_create_upload_url":
            return _Result(
                json.dumps(
                    {
                        "upload_url": "https://files.e2e.test/u/token-value",
                        "file_id": "eph:uploads/1",
                    }
                )
            )
        return _Result(json.dumps({"file_url": "https://files.e2e.test/d/other-token?x=1"}))

    def put(url: str, body: bytes) -> int:
        assert "token-value" in url
        uploads[url] = body
        return 200

    def get(url: str) -> bytes:
        return uploads["https://files.e2e.test/u/token-value"]

    body = b"abc" * 10
    document = mcp_check.run_files(
        list_tools=lambda: [_Tool("files_create_upload_url"), _Tool("files_get_download_link")],
        call_tool=call,
        put=put,
        get=get,
        body=body,
    )
    raw = json.dumps(document)
    assert document["ok"] is True
    assert "?" not in raw
    assert "x=1" not in raw
    assert document["steps"][1]["upload"] == "files.e2e.test/u/token-value"
    assert document["steps"][3]["download"] == "files.e2e.test/d/other-token"


def test_hello_requires_the_service_account_subject() -> None:
    document = mcp_check.run_hello(
        namespace="docs",
        list_tools=lambda: [_Tool("docs_whoami"), _Tool("docs_greet")],
        call_tool=lambda _name, _args: _Result(json.dumps({"sub": "svc:e2e"})),
    )
    assert document["ok"] is True
    with pytest.raises(mcp_check.CheckError):
        mcp_check.run_hello(
            namespace="docs",
            list_tools=lambda: [_Tool("docs_whoami")],
            call_tool=lambda _name, _args: _Result(json.dumps({"sub": "user:ada"})),
        )


def test_absent_rejects_a_leftover_namespace() -> None:
    document = mcp_check.run_absent(
        namespace="files",
        list_tools=lambda: [_Tool("docs_whoami")],
    )
    assert document["ok"] is True
    with pytest.raises(mcp_check.CheckError):
        mcp_check.run_absent(namespace="files", list_tools=lambda: [_Tool("files_get_file_info")])


def test_whoami_sub_reads_json_text() -> None:
    assert mcp_check.whoami_sub('{"sub": "svc:e2e"}') == "svc:e2e"
    assert mcp_check.whoami_sub({"verified": False}) is None


def test_main_refuses_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("E2E_API_KEY", raising=False)
    assert (
        mcp_check.main(
            ["--keystone", "http://127.0.0.1:9/", "--mode", "absent", "--namespace", "files"]
        )
        == 1
    )
    text = capsys.readouterr().out
    assert "E2E_API_KEY is not set" in text
    assert "ksk_" not in text
