"""Command execution. Docker, git, certbot, and nginx are called only through this."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("keystonectl.runner")


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 300,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    env: Mapping[str, str] | None
    cwd: Path | None
    input: str | None
    timeout: float


class RealCommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 300,
    ) -> CommandResult:
        merged = dict(os.environ)
        if env:
            merged.update(env)
        merged["GIT_TERMINAL_PROMPT"] = "0"
        merged["LC_ALL"] = "C"
        log.debug("run %s", list(args))
        try:
            completed = subprocess.run(
                list(args),
                shell=False,
                capture_output=True,
                text=True,
                env=merged,
                cwd=cwd,
                input=input,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            return CommandResult(tuple(args), 124, stdout, "timeout")
        return CommandResult(tuple(args), completed.returncode, completed.stdout, completed.stderr)


RuleOutcome = CommandResult | Callable[..., CommandResult]


class FakeCommandRunner:
    def __init__(self, rules: Sequence[tuple[Sequence[str], RuleOutcome]] | None = None) -> None:
        self.rules = list(rules or [])
        self.calls: list[RecordedCall] = []

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 300,
    ) -> CommandResult:
        recorded = RecordedCall(tuple(args), env, cwd, input, timeout)
        self.calls.append(recorded)
        for prefix, outcome in self.rules:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                if callable(outcome):
                    return outcome(args, env=env, cwd=cwd, input=input, timeout=timeout)
                return outcome
        raise AssertionError(f"no rule for {list(args)}")


def build_runner() -> CommandRunner:
    return RealCommandRunner()
