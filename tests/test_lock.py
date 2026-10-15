"""services.lock.yaml read, backup, and the lock file."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from keystonectl.lock import LockError, empty_lock, hold_lock, load_lock, save_lock
from tests.support import EXAMPLES, mode, write_empty_lock


def test_roundtrip_full_example(tmp_path: Path) -> None:
    source = EXAMPLES / "services.lock.yaml"
    original = source.read_bytes()
    document = load_lock(source)
    dest = tmp_path / "services.lock.yaml"
    save_lock(dest, document)
    assert dest.read_bytes() == original


def test_backup_created_before_write(tmp_path: Path) -> None:
    path = tmp_path / "services.lock.yaml"
    save_lock(path, empty_lock(datetime(2026, 10, 13, tzinfo=UTC)))
    first = path.read_bytes()
    document = load_lock(path)
    document["updated_by"] = "keystonectl 0.1.0-test"
    save_lock(path, document)
    backup = Path(str(path) + ".bak")
    assert backup.read_bytes() == first
    assert mode(backup) == 0o600


def test_atomic_write_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "services.lock.yaml"
    save_lock(path, empty_lock(datetime(2026, 10, 13, tzinfo=UTC)))
    assert mode(path) == 0o600
    assert not list(tmp_path.glob(".services.lock.yaml.tmp"))


def test_unknown_schema_or_layout_version_refused(tmp_path: Path) -> None:
    path = tmp_path / "services.lock.yaml"
    path.write_text(
        "schema_version: 2\nlayout_version: 1\nupdated_at: '2026-10-13T00:00:00Z'\n"
        "updated_by: keystonectl 0.1.0\nlayout: null\nservices: []\nresources: []\n",
        encoding="utf-8",
    )
    with pytest.raises(LockError) as caught:
        load_lock(path)
    assert caught.value.error_id == "lock_corrupt"
    path.write_text(
        "schema_version: 1\nlayout_version: 2\nupdated_at: '2026-10-13T00:00:00Z'\n"
        "updated_by: keystonectl 0.1.0\nlayout: null\nservices: []\nresources: []\n",
        encoding="utf-8",
    )
    with pytest.raises(LockError) as caught:
        load_lock(path)
    assert caught.value.error_id == "layout_unsupported"


def test_schema_rejects_unknown_field(tmp_path: Path) -> None:
    document = empty_lock(datetime(2026, 10, 13, tzinfo=UTC))
    document["services"] = [
        {"name": "file-mcp", "role": "service", "managed": "remote", "secret_key": "nope"}
    ]
    path = tmp_path / "services.lock.yaml"
    with pytest.raises(LockError) as caught:
        save_lock(path, document)
    assert caught.value.error_id == "lock_corrupt"
    assert "secret_key" in caught.value.message or "secret_key" in str(caught.value)


def test_busy_when_locked(tmp_path: Path) -> None:
    path = tmp_path / "services.lock.yaml"
    save_lock(path, empty_lock(datetime(2026, 10, 13, tzinfo=UTC)))
    script = textwrap.dedent(
        """\
        import fcntl, os, time, sys
        path = sys.argv[1] + ".lck"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(30)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(LockError) as caught, hold_lock(path):
            pass
        assert caught.value.error_id == "busy"
        assert caught.value.retryable is True
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_missing_file_is_empty_state(tmp_path: Path) -> None:
    document = load_lock(tmp_path / "services.lock.yaml")
    assert document["schema_version"] == 1
    assert document["services"] == []
    assert document["resources"] == []
    assert not (tmp_path / "services.lock.yaml").exists()


def _hold_lock_externally(path: Path) -> subprocess.Popen:
    """Start a subprocess that holds ``path``'s ``.lck`` file for the whole test."""
    script = textwrap.dedent(
        """\
        import fcntl, os, sys, time
        path = sys.argv[1] + ".lck"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(30)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    return process


def test_cli_service_apply_gets_busy_when_locked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_empty_lock(root)
    holder = _hold_lock_externally(root / "services.lock.yaml")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "keystonectl",
                "service",
                "apply",
                "file-mcp",
                "--root",
                str(root),
                "--json",
                "--non-interactive",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    payload = json.loads(completed.stdout)
    assert completed.returncode == 3
    assert payload["code"] == 3
    assert payload["error"]["id"] == "busy"
    assert payload["retryable"] is True


def test_cli_read_command_works_when_locked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_empty_lock(root)
    holder = _hold_lock_externally(root / "services.lock.yaml")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "keystonectl",
                "service",
                "list",
                "--root",
                str(root),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["code"] == 0
    assert payload["error"] is None


def test_cli_catalog_set_ref_gets_busy_when_locked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_empty_lock(root)
    holder = _hold_lock_externally(root / "services.lock.yaml")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "keystonectl",
                "--root",
                str(root),
                "--json",
                "catalog",
                "set-ref",
                "keystone-codes",
                "v1.0.0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    payload = json.loads(completed.stdout)
    assert completed.returncode == 3
    assert payload["code"] == 3
    assert payload["command"] == "catalog set-ref"
    assert payload["error"]["id"] == "busy"
    assert payload["retryable"] is True
