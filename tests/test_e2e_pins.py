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
        "f73310df8634a19406bf21972e023af9a3b44ac0",
        "5b6c8f5db02a09248c1f12e48e03500c1becb8a1",
        "709d7e69b2e1c9325807fd8544208ccc63873b91",
        "20c1603a7b5339816ac897462b94206d9a00bfa6",
        "sha256:d0aeabb77937cabb2e75fbfa1e3191a771587032bf1b052d900bb53a54073323",
        "sha256:5129e83cd5a3e44760fd71afa5d90cc0fb840e29f19cada263aaff0603464798",
        "sha256:a9dca07071a50045809704bb10681922448c9d7680d36afdbc2f7f759caaea6e",
        "sha256:468a6df578c096e46a8dbff6e133ae5b480cf9fa802fde150a2430b10f63be92",
        "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
        "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
        "sha256:4d6081cb1608c922c6c7299d30570a145187e65e4be274f9bb98aaaa7d67c519",
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
