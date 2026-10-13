"""``keystonectl service status``."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from keystonectl.generate.status import classify_public, run_status
from keystonectl.runner import CommandResult, FakeCommandRunner
from tests.support.deploylab import apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def _status_runner(
    rows: list[dict], *, info_code: int = 0, exec_code: int = 0, probe: str = "status:200"
) -> FakeCommandRunner:
    def docker(args, **_kwargs):
        if info_code != 0:
            return CommandResult(tuple(args), 1, "", "Cannot connect to the Docker daemon")
        if args[0] == "docker" and args[1] == "info":
            return CommandResult(tuple(args), 0, "ok\n", "")
        if "config" in args:
            return CommandResult(tuple(args), 0, json.dumps({"name": "demo", "services": {}}), "")
        if "ps" in args:
            return CommandResult(
                tuple(args), 0, "".join(json.dumps(row) + "\n" for row in rows), ""
            )
        if "exec" in args:
            return CommandResult(tuple(args), exec_code, "", "")
        if args[1] == "run":
            return CommandResult(tuple(args), 0, probe + "\n", "")
        if "logs" in args:
            return CommandResult(
                tuple(args),
                0,
                "upstream discovery: 0 of 1 upstream(s) unavailable (none); proxying the rest\n",
                "",
            )
        return CommandResult(tuple(args), 0, "", "")

    return FakeCommandRunner([(["docker"], docker)])


def test_status_all_containers_and_init_completed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    text = (PACKAGES / "docs-service" / "keystone-service.yaml").read_text()
    document = yaml.safe_load(text)
    document.pop("needs", None)
    result, _runner, _box = apply_package(
        root, "docs-service", {"docs-service": yaml.safe_dump(document)}
    )
    assert result.code == 0, result.error
    rows = [
        {"Service": "docs-service-migrate", "State": "exited", "ExitCode": 0, "Health": ""},
        {"Service": "docs-service-api", "State": "running", "ExitCode": 0, "Health": "healthy"},
        {"Service": "docs-service-mcp", "State": "running", "ExitCode": 0, "Health": "healthy"},
    ]
    status = run_status(root, _status_runner(rows))
    assert status.code == 0
    service = status.result["services"][0]
    assert service["problems"] == []
    assert len(service["containers"]) == 3


def test_status_init_failed_is_problem(tmp_path: Path) -> None:
    root = tmp_path / "root"
    text = (PACKAGES / "docs-service" / "keystone-service.yaml").read_text()
    document = yaml.safe_load(text)
    document.pop("needs", None)
    apply_package(root, "docs-service", {"docs-service": yaml.safe_dump(document)})
    rows = [
        {"Service": "docs-service-migrate", "State": "exited", "ExitCode": 1, "Health": ""},
        {"Service": "docs-service-api", "State": "running", "ExitCode": 0, "Health": "healthy"},
        {"Service": "docs-service-mcp", "State": "running", "ExitCode": 0, "Health": "healthy"},
    ]
    status = run_status(root, _status_runner(rows))
    ids = [item["id"] for item in status.result["services"][0]["problems"]]
    assert "init_failed" in ids


def test_status_container_missing_is_problem(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    status = run_status(root, _status_runner([]))
    ids = [item["id"] for item in status.result["services"][0]["problems"]]
    assert "container_missing" in ids


def test_status_keystone_readyz_and_discovery(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    rows = [{"Service": "file-mcp-mcp", "State": "running", "ExitCode": 0, "Health": "healthy"}]
    status = run_status(root, _status_runner(rows, exec_code=0))
    keystone = status.result["services"][0]["keystone"]
    assert keystone["ready"] == "ready"
    assert keystone["discovery"] in {"available", "unknown"}


def test_status_inside_check_mapping() -> None:
    assert classify_public("status:200")[0] == "ok"
    inside, warning, problem = classify_public(
        "error:SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED"
    )
    assert inside == "ok" and warning["id"] == "tls_untrusted" and problem is None
    _inside, _warning, problem = classify_public("status:502")
    assert problem["id"] == "public_upstream_down"
    _inside, _warning, problem = classify_public("error:URLError: connection refused")
    assert problem["id"] == "hairpin_failed"


def test_status_remote_entry_keystone_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    entry = lock["services"][0]
    entry["managed"] = "remote"
    for field in (
        "package",
        "version",
        "ref",
        "commit",
        "containers",
        "fragment",
        "fragment_sha256",
    ):
        entry.pop(field, None)
    (root / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    status = run_status(root, _status_runner([]))
    service = status.result["services"][0]
    assert service["containers"] == []
    assert service["keystone"]["ready"] in {"ready", "down"}


def test_status_docker_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    status = run_status(root, _status_runner([], info_code=1))
    assert status.code == 3
    assert status.error["id"] == "docker_unavailable"
    assert status.retryable is True
