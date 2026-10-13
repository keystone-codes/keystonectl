"""Real openssl pairs. Skipped when the openssl binary is not installed."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from keystonectl.runner import RealCommandRunner
from keystonectl.tls import Tls, TlsError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")

CLOCK = datetime(2026, 10, 13, tzinfo=UTC)


def _req(directory: Path, kind: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / f"{kind}.pem"
    key = directory / f"{kind}.key"
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048" if kind == "rsa" else "ec",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        "30",
        "-nodes",
        "-subj",
        "/CN=files.example.com",
    ]
    if kind == "ec":
        command[4:4] = ["-pkeyopt", "ec_paramgen_curve:prime256v1"]
        # -newkey ec was shifted; rebuild so the curve option sits with -newkey.
        command = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "30",
            "-nodes",
            "-subj",
            "/CN=files.example.com",
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return cert, key


def _tls(tmp_path: Path) -> Tls:
    return Tls(
        RealCommandRunner(),
        clock=lambda: CLOCK,
        le_live=tmp_path / "le",
        custom_dir=tmp_path / "custom",
        cron_file=tmp_path / "cron",
        is_root=True,
    )


def _installed_paths(tmp_path: Path, cert: Path, key: Path) -> tuple[Path, Path]:
    """Point at the destination so a matching rerun does not call ``install -o root``."""

    destination = tmp_path / "custom" / "files.example.com"
    destination.mkdir(parents=True)
    target_cert = destination / "fullchain.pem"
    target_key = destination / "privkey.pem"
    target_cert.write_bytes(cert.read_bytes())
    target_key.write_bytes(key.read_bytes())
    return target_cert, target_key


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_real_pair_matches(tmp_path: Path, kind: str) -> None:
    cert, key = _req(tmp_path, kind)
    target_cert, target_key = _installed_paths(tmp_path, cert, key)
    info = _tls(tmp_path).install_custom("files.example.com", target_cert, target_key)
    assert info.kind == "custom"
    assert info.not_after is not None


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_real_mismatch_detected(tmp_path: Path, kind: str) -> None:
    cert, _key = _req(tmp_path, kind)
    _other, wrong = _req(tmp_path / "other", kind)
    target_cert, target_key = _installed_paths(tmp_path, cert, wrong)
    with pytest.raises(TlsError) as caught:
        _tls(tmp_path).install_custom("files.example.com", target_cert, target_key)
    assert caught.value.error_id == "key_mismatch"


def test_real_enddate_parsed(tmp_path: Path) -> None:
    cert, key = _req(tmp_path, "rsa")
    live = tmp_path / "le" / "files.example.com"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_bytes(cert.read_bytes())
    (live / "privkey.pem").write_bytes(key.read_bytes())
    info = _tls(tmp_path).detect("files.example.com")
    assert info.not_after is not None
    assert info.days_left is not None and info.days_left >= 0
