"""The e2e script pins the published release and does not patch layout A."""

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
        "6b04a198101dac22815b17d5bcf55b0e1c6463d8",
        "ffd719701f3bb0eb53bdd212447bcd43bbd07c43",
        "1bc59512acccbd30cfbd958f78b713b21a7276a2",
        "sha256:5976b83fe6b256d66246044c99fb769fff53981104bb1a769e8799bb37fef4c0",
        "sha256:f73a378bd40c3a176df5c7cf560464827d2a9b2c38238da6674342a73bff5337",
        "sha256:af8333b0a5e9209abbb677c546b595dec87b45a227f33a2757b60e7d0ec5717f",
        "sha256:63ba7ba9b7ba59af56cb52d9052eb385cce4e8531b40d6b43639c9e7e514c97f",
        "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
        "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
    ]
    for pin in pins:
        assert pin in SCRIPT
    assert (ROOT / "scripts" / "e2e.sh").is_file()
    assert ":latest" not in SCRIPT
