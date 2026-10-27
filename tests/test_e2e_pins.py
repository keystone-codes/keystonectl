"""The e2e script pins the published release and does not patch layout A."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "e2e.sh").read_text(encoding="utf-8")
PATCH = ROOT / "scripts" / "e2e" / "fixtures" / "production-example.patch"


def test_layout_a_patch_has_no_diff() -> None:
    lines = PATCH.read_text(encoding="utf-8").splitlines()
    assert lines
    assert not any(line.startswith(("diff ", "---", "+++")) for line in lines)


def test_script_pins_published_refs() -> None:
    pins = [
        "5d7084ddc2fd65fc5377e66710781d9e9cd613b3",
        "5b6c8f5db02a09248c1f12e48e03500c1becb8a1",
        "df995ba0d8dc8d3e807545a325fca79d2e8d91c7",
        "855589babe7e9c7212b60654855b7ccd4086f53a",
        "sha256:d0aeabb77937cabb2e75fbfa1e3191a771587032bf1b052d900bb53a54073323",
        "sha256:5129e83cd5a3e44760fd71afa5d90cc0fb840e29f19cada263aaff0603464798",
        "sha256:a9dca07071a50045809704bb10681922448c9d7680d36afdbc2f7f759caaea6e",
        "sha256:468a6df578c096e46a8dbff6e133ae5b480cf9fa802fde150a2430b10f63be92",
        "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
        "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
        "sha256:fe00911601f626a333897c42efa35f513b8984ca6a73f0dae7809d6f2c7e2ac9",
    ]
    for pin in pins:
        assert pin in SCRIPT
    assert (ROOT / "scripts" / "e2e.sh").is_file()
    assert ":latest" not in SCRIPT


def test_atlassian_mock_matches_sha256() -> None:
    fixture = ROOT / "scripts" / "e2e" / "fixtures" / "atlassian-mock"
    digest = (fixture / "SHA256").read_text(encoding="utf-8").strip()
    actual = hashlib.sha256((fixture / "atlassian_mock.py").read_bytes()).hexdigest()
    assert actual == digest
