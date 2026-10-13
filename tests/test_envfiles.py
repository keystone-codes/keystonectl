"""Env files read the same way from Compose and from bash."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from keystonectl.envfiles import read_env, upsert_env
from tests.support import mode


def test_upsert_keeps_other_lines_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "pkg.env"
    path.write_text("# keep me\nKEEP=yes\nOTHER=1\n", encoding="utf-8")
    upsert_env(path, {"OTHER": "2", "NEW": "added"})
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "KEEP=yes" in text
    found = read_env(path)
    assert found["KEEP"] == "yes"
    assert found["OTHER"] == "2"
    assert found["NEW"] == "added"


def test_quoting_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "pkg.env"
    values = {
        "PLAIN": "abc",
        "SPACE": "a b",
        "HASH": "a#b",
        "QUOTE": 'say "hi"',
        "DOLLAR": "cost $5",
        "TICK": "run `id`",
    }
    upsert_env(path, values)
    assert read_env(path) == values
    bash = shutil.which("bash")
    if bash is None:
        return
    completed = subprocess.run(
        [bash, "-c", 'set -a; . "$1"; env', "bash", str(path)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    parsed: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    for key, value in values.items():
        assert parsed[key] == value


def test_newline_refused(tmp_path: Path) -> None:
    path = tmp_path / "pkg.env"
    with pytest.raises(ValueError):
        upsert_env(path, {"BROKEN": "one\ntwo"})


def test_mode_600_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "pkg.env"
    upsert_env(path, {"KEEP": "yes"})
    assert mode(path) == 0o600

    def refuse_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse_replace)
    with pytest.raises(OSError):
        upsert_env(path, {"KEEP": "no"})
    assert path.read_text(encoding="utf-8") == "KEEP=yes\n"
    leftovers = [item.name for item in tmp_path.iterdir() if item.name != path.name]
    assert leftovers == []
