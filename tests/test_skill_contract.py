"""The keystonectl skill stays aligned with the CLI, registries, and output schema."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

from keystonectl.answers import QUESTION_TEMPLATES
from keystonectl.cli import build_parser
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.resources import schema_text
from tests.skill_text import iter_commands, scenario_bodies, section, table_rows

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "keystonectl" / "SKILL.md"
FIXTURES = ROOT / "skills" / "keystonectl" / "tests" / "fixtures"
SCHEMA = json.loads(schema_text("output.schema.json"))

_FORBIDDEN = ("docker", "nginx", "certbot", "systemctl", "psql", "mc", "cat", "sed", "vi", "nano")
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,}")
_PASSWORD_URL = re.compile(r"postgresql://[^/\s]*:[^/\s@]*@")
_SCENARIOS = {
    "file-mcp-minio",
    "docs-service-external-postgres",
    "dns-retry",
    "eject",
    "remove",
}


def _looks_random(token: str) -> bool:
    if token.startswith("KEYSTONE_SETUP_"):
        return False
    return (
        any(char.islower() for char in token)
        and any(char.isupper() for char in token)
        and any(char.isdigit() for char in token)
    )


def test_skill_commands_parse() -> None:
    commands = iter_commands(SKILL.read_text(encoding="utf-8"))
    assert commands
    for argv in commands:
        parser = build_parser()
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            raise AssertionError(argv) from exc


def test_skill_uses_json_everywhere() -> None:
    commands = iter_commands(SKILL.read_text(encoding="utf-8"))
    assert commands
    for argv in commands:
        if "--version" in argv:
            continue
        assert "--json" in argv, argv


def test_skill_error_table_matches_registry() -> None:
    text = SKILL.read_text(encoding="utf-8")
    errors: dict[str, tuple[int, bool]] = {}
    for cells in table_rows(section(text, "Errors")):
        errors[cells[0].strip("`")] = (int(cells[1]), cells[2] == "true")
    assert set(errors) == set(ERRORS)
    for error_id, spec in ERRORS.items():
        assert errors[error_id] == (spec.code, spec.retryable)

    warnings: dict[str, tuple[int, bool]] = {}
    for cells in table_rows(section(text, "Warnings")):
        warnings[cells[0].strip("`")] = (int(cells[1]), cells[2] == "true")
    assert set(warnings) == set(WARNINGS)
    for warning_id in WARNINGS:
        assert warnings[warning_id] == (0, False)


def test_skill_var_table_covers_questions() -> None:
    body = section(SKILL.read_text(encoding="utf-8"), "Questions")
    for var in QUESTION_TEMPLATES:
        assert var in body, var


def test_skill_has_no_foreign_commands() -> None:
    bodies = scenario_bodies(SKILL.read_text(encoding="utf-8"))
    assert set(bodies) == _SCENARIOS
    for name, body in bodies.items():
        exempt_next = False
        for line in body.splitlines():
            stripped = line.strip()
            if "<!-- operator-command -->" in stripped:
                rest = stripped.split("-->", 1)[-1].strip()
                exempt_next = not rest
                continue
            if exempt_next:
                exempt_next = False
                continue
            if not stripped or stripped.startswith(("#", "Agent:", "Operator:")):
                continue
            first = stripped.split()[0]
            if "=" in first and not first.startswith("keystonectl"):
                continue
            assert first not in _FORBIDDEN, f"{name}: {stripped}"


def test_skill_has_no_secret_like_values() -> None:
    paths = [SKILL, *sorted(path for path in FIXTURES.rglob("*") if path.is_file())]
    assert len(paths) > 1
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "ksk_" not in text
        assert "BEGIN PRIVATE KEY" not in text
        assert _PASSWORD_URL.search(text) is None
        for token in _SECRET_TOKEN.findall(text):
            assert not _looks_random(token), f"{path}: {token}"


def test_fixtures_match_output_schema() -> None:
    validator = jsonschema.Draft202012Validator(SCHEMA)
    files = sorted(FIXTURES.rglob("*.json"))
    assert files
    for path in files:
        validator.validate(json.loads(path.read_text(encoding="utf-8")))
