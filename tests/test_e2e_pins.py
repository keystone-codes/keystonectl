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
        "dd4538167af85bf5842d88e43bca53748063879f",
        "1bc59512acccbd30cfbd958f78b713b21a7276a2",
        "sha256:5976b83fe6b256d66246044c99fb769fff53981104bb1a769e8799bb37fef4c0",
        "sha256:f73a378bd40c3a176df5c7cf560464827d2a9b2c38238da6674342a73bff5337",
        "sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e",
        "sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727",
        "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
        "sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
    ]
    for pin in pins:
        assert pin in SCRIPT
    assert (ROOT / "scripts" / "e2e.sh").is_file()
    assert ":latest" not in SCRIPT
