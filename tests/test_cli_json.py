"""JSON output, exit codes, secrets, and root detection."""

from __future__ import annotations

import io
import json
from pathlib import Path

import jsonschema
import pytest

from keystonectl import __version__
from keystonectl.answers import Question, read_secret_file
from keystonectl.cli import main
from keystonectl.output import Result, SecretRegistry, emit
from keystonectl.plan import StepPlan, register_step
from keystonectl.resources import schema_text
from keystonectl.runner import CommandResult
from tests.support import file_hashes, git_runner, seed_catalog


def _run(argv: list[str]) -> tuple[int, str]:
    code = main(argv)
    return code, ""


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    document = json.loads(capsys.readouterr().out)
    schema = json.loads(schema_text("output.schema.json"))
    jsonschema.Draft202012Validator(schema).validate(document)
    return code, document


def test_json_success_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("keystonectl.cli.build_runner", git_runner)
    code, document = _json(capsys, ["--json", "--root", str(tmp_path), "catalog", "list"])
    assert code == 0
    assert document["ok"] is True
    assert document["command"] == "catalog list"
    assert document["error"] is None
    assert document["retryable"] is False


def test_json_missing_exit_2_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Ask:
        id = "ask"

        def plan(self, ctx: object) -> StepPlan:
            return StepPlan(questions=[Question("DEMO_NAME", "What is the name?", "text")])

        def apply(self, ctx: object) -> None:
            return None

    register_step(Ask(), after="resolve")
    seed_catalog(tmp_path)
    monkeypatch.setenv("KEYSTONE_SETUP_KEYSTONE_MCP_VERSION", "0.5.0")
    before = file_hashes(tmp_path)
    code, document = _json(
        capsys, ["--json", "--root", str(tmp_path), "service", "plan", "file-mcp"]
    )
    assert code == 2
    assert document["missing"][0]["var"] == "DEMO_NAME"
    assert document["changes"] == 0
    assert file_hashes(tmp_path) == before


def test_json_external_error_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from keystonectl.runner import FakeCommandRunner

    runner = FakeCommandRunner(
        [(["git", "ls-remote"], CommandResult((), 1, "", "ls-remote failed"))]
    )
    monkeypatch.setattr("keystonectl.cli.build_runner", lambda: runner)
    code, document = _json(
        capsys,
        ["--json", "--root", str(tmp_path), "catalog", "add", "https://example.com/catalog.git"],
    )
    assert code == 3
    assert document["ok"] is False
    assert document["error"]["id"] == "source_unreachable"
    assert document["retryable"] is True


def test_usage_error_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _json(capsys, ["--json", "nope"])
    assert code == 1
    assert document["error"]["id"] == "usage"


def test_set_refuses_secret(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "super-secret-value"
    code = main(["--json", "--set", f"TOKEN_FILE={secret}", "explain", "usage"])
    text = capsys.readouterr().out
    document = json.loads(text)
    assert code == 1
    assert document["error"]["id"] == "secret_in_argument"
    assert secret not in text


def test_secret_never_in_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "s3cret-value"
    path = tmp_path / "token"
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    secrets = SecretRegistry()
    read_secret_file(path, secrets)
    emit(
        Result(code=0, command="check", summary=f"saw {secret}", changes=0),
        json_mode=True,
        secrets=secrets,
        stdout=io.StringIO(),
    )
    buffer = io.StringIO()
    emit(
        Result(code=0, command="check", summary=f"saw {secret}", changes=0),
        json_mode=True,
        secrets=secrets,
        stdout=buffer,
    )
    text = buffer.getvalue()
    assert secret not in text
    assert "***" in text
    assert secret not in capsys.readouterr().out


def test_explain_known_and_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _json(capsys, ["--json", "explain", "usage"])
    assert code == 0
    assert document["result"]["id"] == "usage"
    assert document["result"]["title"]
    code, document = _json(capsys, ["--json", "explain", "no-such-id"])
    assert code == 1
    assert document["error"]["id"] == "usage"


def test_text_mode_same_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "usage"]) == 0
    assert main(["--json", "explain", "usage"]) == 0
    capsys.readouterr()
    assert main(["explain", "no-such-id"]) == 1
    assert main(["--json", "explain", "no-such-id"]) == 1


def test_apply_without_deploy_step_not_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deploy step exists now. The seeded clone is not a valid package manifest."""

    seed_catalog(tmp_path)
    monkeypatch.setattr("keystonectl.cli.build_runner", git_runner)
    monkeypatch.setenv("KEYSTONE_SETUP_KEYSTONE_MCP_VERSION", "0.5.0")
    code, document = _json(
        capsys, ["--json", "--root", str(tmp_path), "service", "apply", "file-mcp"]
    )
    assert code == 3
    assert document["error"]["id"] == "manifest_invalid"
    assert document["changes"] == 0


def test_root_detection_env_and_upward_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "deploy"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    from tests.support import write_empty_lock

    write_empty_lock(root)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("KEYSTONECTL_ROOT", raising=False)
    code, document = _json(capsys, ["--json", "service", "list"])
    assert code == 0
    assert document["command"] == "service list"

    other = tmp_path / "other"
    write_empty_lock(other)
    monkeypatch.setenv("KEYSTONECTL_ROOT", str(other))
    code, document = _json(capsys, ["--json", "service", "list"])
    assert code == 0

    monkeypatch.delenv("KEYSTONECTL_ROOT", raising=False)
    monkeypatch.chdir(tmp_path / "empty" if False else tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    code, document = _json(capsys, ["--json", "service", "list"])
    assert code == 1
    assert document["error"]["id"] == "root_not_found"
    assert "init --adopt" in document["error"]["message"]


def test_version_flag_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"keystonectl {__version__}\n"
    code, document = _json(capsys, ["--version", "--json"])
    assert code == 0
    assert document["result"]["version"] == __version__


def test_schemas_load_via_importlib_resources() -> None:
    import inspect

    from keystonectl import resources

    source = inspect.getsource(resources)
    assert "importlib.resources" in source
    assert "2020-12" in schema_text("output.schema.json")
    assert schema_text("service-package-v1.json").lstrip().startswith("{")
    assert schema_text("lock-v1.json").lstrip().startswith("{")


def test_internal_error_is_json_with_code_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(error_id: str) -> Result:
        raise RuntimeError("boom")

    monkeypatch.setattr("keystonectl.cli._explain", boom)
    code, document = _json(capsys, ["--json", "explain", "usage"])
    assert code == 3
    assert document["ok"] is False
    assert document["error"]["id"] == "internal_error"
    assert document["error"]["message"] == "RuntimeError: boom"
    assert document["retryable"] is False


def test_internal_error_text_mode_one_line_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(error_id: str) -> Result:
        raise RuntimeError("boom")

    monkeypatch.setattr("keystonectl.cli._explain", boom)
    code = main(["explain", "usage"])
    assert code == 3
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "internal_error: RuntimeError: boom"
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_internal_error_traceback_only_with_debug_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(error_id: str) -> Result:
        raise RuntimeError("boom")

    monkeypatch.setattr("keystonectl.cli._explain", boom)
    monkeypatch.setenv("KEYSTONECTL_DEBUG", "1")
    code = main(["explain", "usage"])
    assert code == 3
    assert "Traceback" in capsys.readouterr().err


def test_internal_error_does_not_catch_system_exit_or_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(error_id: str) -> Result:
        raise SystemExit(5)

    monkeypatch.setattr("keystonectl.cli._explain", boom)
    with pytest.raises(SystemExit):
        main(["explain", "usage"])

    def interrupt(error_id: str) -> Result:
        raise KeyboardInterrupt

    monkeypatch.setattr("keystonectl.cli._explain", interrupt)
    with pytest.raises(KeyboardInterrupt):
        main(["explain", "usage"])
