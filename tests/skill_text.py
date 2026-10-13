"""Shared readers for the keystonectl skill text."""

from __future__ import annotations

import re
import shlex

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_SCENARIO = re.compile(
    r"<!-- scenario: ([a-z0-9-]+) -->\n(.*?)<!-- /scenario -->",
    re.DOTALL,
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def logical_lines(text: str) -> list[str]:
    """Join backslash continuations the way a shell would."""

    logical: list[str] = []
    buf = ""
    for line in text.splitlines():
        if buf:
            buf = buf[:-1].rstrip() + " " + line.strip()
        else:
            buf = line
        if buf.rstrip().endswith("\\"):
            continue
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)
    return logical


def command_argv(line: str) -> list[str] | None:
    """Arguments of a keystonectl command, after leading VAR=value tokens."""

    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        return None
    while tokens and _ENV_ASSIGN.match(tokens[0]):
        tokens.pop(0)
    if not tokens or tokens[0] != "keystonectl":
        return None
    return tokens[1:]


def iter_commands(text: str) -> list[list[str]]:
    found: list[list[str]] = []
    for block in _FENCE.findall(text):
        for line in logical_lines(block):
            argv = command_argv(line)
            if argv is not None:
                found.append(argv)
    return found


def scenario_bodies(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in _SCENARIO.finditer(text)}


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing section {heading}")
    return match.group(1)


def table_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        head = cells[0].strip("`")
        if head in {"id", "Variable", ""} or head.startswith("-"):
            continue
        rows.append(cells)
    return rows
