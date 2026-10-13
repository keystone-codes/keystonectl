"""Regenerate the Questions and Errors tables in docs/commands.md."""

from __future__ import annotations

import sys
from pathlib import Path

from keystonectl.answers import QUESTION_TEMPLATES
from keystonectl.errors import ERRORS, WARNINGS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "commands.md"
START = "<!-- generated:questions -->"
END = "<!-- /generated -->"


def _cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def _owner(var: str) -> str:
    if var == "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION":
        return "I01"
    if "NAMESPACE" in var or "_ENV_" in var or "_SECRET_" in var:
        return "I03"
    if var.startswith(
        ("KEYSTONE_SETUP_RESOURCE_", "KEYSTONE_SETUP_S3_", "KEYSTONE_SETUP_POSTGRES_")
    ):
        return "I02"
    if any(part in var for part in ("TLS", "DOMAIN", "CERT", "CONFIRM", "PUBLIC")):
        return "I04"
    return "I01"


def render() -> str:
    questions = [
        "| Variable | Kind | When | Owner |",
        "|---|---|---|---|",
    ]
    for var in sorted(QUESTION_TEMPLATES):
        question = QUESTION_TEMPLATES[var]
        questions.append(
            f"| {_cell(var)} | {_cell(question.kind)} | {_cell(question.text)} | {_owner(var)} |"
        )
    errors = [
        "| Id | Code | Retryable | Action |",
        "|---|---|---|---|",
    ]
    for error_id in sorted(ERRORS):
        spec = ERRORS[error_id]
        retryable = "true" if spec.retryable else "false"
        errors.append(f"| {spec.id} | {spec.code} | {retryable} | {_cell(spec.action)} |")
    for warning_id in sorted(WARNINGS):
        spec = WARNINGS[warning_id]
        errors.append(f"| {spec.id} | warning | false | {_cell(spec.action)} |")
    return "\n".join(questions) + "\n\n" + "\n".join(errors) + "\n"


def splice(text: str, block: str) -> str:
    if START not in text or END not in text:
        raise SystemExit(f"{DOC} is missing the generated-table markers")
    before, rest = text.split(START, 1)
    _old, after = rest.split(END, 1)
    return f"{before}{START}\n{block}{END}{after}"


def main(argv: list[str]) -> int:
    text = DOC.read_text(encoding="utf-8")
    updated = splice(text, render())
    if "--check" in argv:
        if updated != text:
            print(
                "docs/commands.md tables are stale. Run scripts/gen_commands_tables.py.",
                file=sys.stderr,
            )
            return 1
        return 0
    DOC.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
