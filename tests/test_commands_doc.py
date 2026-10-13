"""docs/commands.md stays aligned with the parser, registries, and output schema."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import jsonschema

from keystonectl.answers import QUESTION_TEMPLATES
from keystonectl.cli import build_parser
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.resources import schema_text

DOC = Path("docs/commands.md")
SCHEMA = json.loads(schema_text("output.schema.json"))


def _commands() -> set[str]:
    found: set[str] = set()

    def walk(parser, prefix: str) -> None:
        for action in parser._actions:
            if action.__class__.__name__ != "_SubParsersAction":
                continue
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                found.add(path)
                walk(sub, path)

    walk(build_parser(), "keystonectl")
    return found


def _headers(text: str) -> set[str]:
    return set(re.findall(r"^### (keystonectl(?: [a-z0-9-]+)*)$", text, flags=re.MULTILINE))


def test_every_subcommand_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert _headers(text) == _commands()


def test_every_error_and_warning_id_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    for spec in ERRORS.values():
        retryable = "true" if spec.retryable else "false"
        assert f"| {spec.id} | {spec.code} | {retryable} |" in text
    for spec in WARNINGS.values():
        assert f"| {spec.id} | warning | false |" in text


def test_every_question_template_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    for var, question in QUESTION_TEMPLATES.items():
        assert f"| {var} | {question.kind} |" in text


def test_json_examples_validate_against_output_schema() -> None:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    assert blocks
    for block in blocks:
        jsonschema.Draft202012Validator(SCHEMA).validate(json.loads(block))


def test_generated_tables_up_to_date() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/gen_commands_tables.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
