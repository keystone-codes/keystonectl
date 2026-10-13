#!/usr/bin/env python3
"""Check a fake keystonectl journal against one skill scenario."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
SKILL = TESTS.parent / "SKILL.md"
EXTRACT = TESTS / "extract-scenario.sh"

sys.path.insert(0, str(ROOT))
from tests.skill_text import command_argv, logical_lines


def scenario_text(name: str) -> str:
    return subprocess.check_output([str(EXTRACT), name, str(SKILL)], text=True)


def expected_argvs(name: str) -> list[list[str]]:
    found: list[list[str]] = []
    for line in logical_lines(scenario_text(name)):
        argv = command_argv(line)
        if argv is not None:
            found.append(argv)
    if not found:
        raise SystemExit(f"{name}: no keystonectl commands")
    return found


def secret_values() -> list[str]:
    values: list[str] = []
    for path in sorted(FIXTURES.rglob("secrets/*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            values.append(text)
    return values


def main() -> None:
    name = sys.argv[1]
    journal_path = Path(sys.argv[2])
    records = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected = expected_argvs(name)
    actual = [record["argv"] for record in records]
    if actual != expected:
        raise SystemExit(f"{name}: call sequence\nexpected {expected}\nactual {actual}")

    journal_text = journal_path.read_text(encoding="utf-8")
    for secret in secret_values():
        if secret in journal_text:
            raise SystemExit(f"{name}: secret file contents reached the journal")
        for record in records:
            for arg in record["argv"]:
                if secret in arg:
                    raise SystemExit(f"{name}: secret file contents reached an argument")
            for value in record["env"].values():
                if secret in str(value):
                    raise SystemExit(f"{name}: secret file contents reached an environment value")

    for record in records:
        for var, info in record["files"].items():
            if not info["exists"] or int(str(info["mode"]), 8) != 0o600:
                raise SystemExit(f"{name}: {var} is not a mode 600 file")

    for index, record in enumerate(records):
        fixture_path = FIXTURES / name / f"{record['step']}.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if fixture["code"] != 3 or fixture["retryable"] is not False:
            continue
        for later in records[index + 1 :]:
            if later["argv"] != record["argv"]:
                continue
            changed = later["env"] != record["env"] or later["files"] != record["files"]
            if not changed:
                raise SystemExit(f"{name}: repeated a non-retryable command without a new answer")
            break

    if name == "eject":
        text = scenario_text(name)
        eject_at = text.find("service eject")
        if eject_at < 0:
            raise SystemExit("eject: command missing")
        before = text[:eject_at]
        if "upgrade" not in before.lower() or "adopt" not in before.lower():
            raise SystemExit("eject: consequences were not explained before the command")
        if re.search(r"Operator:\s*Yes", before) is None:
            raise SystemExit("eject: confirmation missing before the command")

    print(f"{name}: ok ({len(records)} calls)")


if __name__ == "__main__":
    main()
