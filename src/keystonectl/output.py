"""Text and JSON results. Every secret seen in this process is redacted."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from keystonectl.errors import ErrorSpec


@dataclass
class SecretRegistry:
    """Values read from secret files, or generated as secrets, in this process."""

    values: list[str] = field(default_factory=list)

    def register(self, value: str) -> None:
        if value and value not in self.values:
            self.values.append(value)

    def redact(self, text: str) -> str:
        redacted = text
        for value in sorted(self.values, key=len, reverse=True):
            if value:
                redacted = redacted.replace(value, "***")
        return redacted


@dataclass
class Result:
    code: int
    command: str
    summary: str
    target: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    changes: int | None = None
    missing: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None
    retryable: bool = False
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        changes = self.changes
        if changes is None:
            wanted = "planned" if self.command.endswith("plan") else "done"
            changes = sum(1 for action in self.actions if action.get("status") == wanted)
        return {
            "ok": self.code == 0,
            "code": self.code,
            "command": self.command,
            "target": self.target,
            "summary": self.summary,
            "actions": self.actions,
            "changes": changes,
            "missing": self.missing,
            "warnings": self.warnings,
            "error": self.error,
            "retryable": bool(self.retryable) if self.code == 3 else False,
            "result": self.result,
        }


def missing_summary(count: int) -> str:
    noun = "answer is" if count == 1 else "answers are"
    return f"{count} {noun} missing"


def error_payload(spec: ErrorSpec, message: str | None = None, hint: str | None = None) -> dict:
    return {
        "id": spec.id,
        "message": message if message is not None else spec.title,
        "hint": hint if hint is not None else spec.action,
    }


def emit(
    result: Result,
    *,
    json_mode: bool,
    secrets: SecretRegistry,
    stdout: TextIO | None = None,
) -> None:
    stream = stdout or sys.stdout
    if json_mode:
        raw = json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n"
    else:
        raw = render_text(result)
    stream.write(secrets.redact(raw))


def render_text(result: Result) -> str:
    if result.command == "version" and result.code == 0:
        version = result.result.get("version", "")
        return f"keystonectl {version}\n"
    lines: list[str] = []
    if result.error:
        lines.append(f"{result.error['id']}: {result.error['message']}")
        hint = result.error.get("hint") or ""
        if hint:
            lines.append(hint)
    elif result.missing:
        lines.append(result.summary)
        for item in result.missing:
            lines.append(f"  {item['var']}: {item['question']}")
    else:
        lines.append(result.summary)
    for warning in result.warnings:
        lines.append(f"warning {warning['id']}: {warning['message']}")
    for action in result.actions:
        lines.append(f"{action['status']}: {action['title']}")
    compose = result.result.get("compose_command")
    if isinstance(compose, str) and compose:
        lines.append(compose)
    if not lines:
        lines.append(result.summary)
    return "\n".join(lines) + "\n"
