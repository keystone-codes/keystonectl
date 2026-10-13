"""Questions, answer lookup, and KEYSTONE_SETUP_* names."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from keystonectl.errors import ERRORS
from keystonectl.output import SecretRegistry


@dataclass(frozen=True)
class Question:
    var: str
    text: str
    kind: str
    choices: tuple[str, ...] | None = None
    default: str | None = None
    secret: bool = False
    help: str | None = None

    def as_missing(self) -> dict:
        return {
            "var": self.var,
            "question": self.text,
            "kind": self.kind,
            "choices": list(self.choices) if self.choices is not None else None,
            "default": self.default,
            "secret": self.secret,
        }


QUESTION_TEMPLATES: dict[str, Question] = {
    "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": Question(
        "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION",
        "Which keystone-mcp version runs here (for example 0.5.0)?",
        "text",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_ENV_{NAME}": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_ENV_{NAME}",
        "Value of {NAME} for package {PKG}.",
        "text",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_SECRET_{NAME}_FILE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_SECRET_{NAME}_FILE",
        "Path to a mode 600 file holding secret {NAME} for package {PKG}.",
        "file",
        secret=True,
    ),
    "KEYSTONE_SETUP_RESOURCE_S3": Question(
        "KEYSTONE_SETUP_RESOURCE_S3",
        "Where should the package keep files: in S3 storage you already have (external), "
        "or in a new MinIO on this server (catalog)?",
        "choice",
        choices=("external", "catalog"),
        help="Answer external or catalog.",
    ),
    "KEYSTONE_SETUP_RESOURCE_POSTGRES": Question(
        "KEYSTONE_SETUP_RESOURCE_POSTGRES",
        "Which PostgreSQL should the package use: a server you already have (external), "
        "or a new PostgreSQL on this server (catalog)?",
        "choice",
        choices=("external", "catalog"),
        help="Answer external or catalog.",
    ),
    "KEYSTONE_SETUP_RESOURCE_S3_PACKAGE": Question(
        "KEYSTONE_SETUP_RESOURCE_S3_PACKAGE",
        "Which S3 provider from the catalogs should be installed?",
        "choice",
        help="Answer source/name, for example keystone-codes/minio.",
    ),
    "KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE": Question(
        "KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE",
        "Which PostgreSQL provider from the catalogs should be installed?",
        "choice",
        help="Answer source/name, for example keystone-codes/postgres.",
    ),
    "KEYSTONE_SETUP_S3_ENDPOINT": Question(
        "KEYSTONE_SETUP_S3_ENDPOINT",
        "S3 endpoint URL (for example https://s3.amazonaws.com).",
        "text",
        help="Example: https://s3.amazonaws.com",
    ),
    "KEYSTONE_SETUP_S3_REGION": Question(
        "KEYSTONE_SETUP_S3_REGION",
        "S3 region (for example us-east-1).",
        "text",
        default="us-east-1",
        help="Example: us-east-1",
    ),
    "KEYSTONE_SETUP_S3_PATH_STYLE": Question(
        "KEYSTONE_SETUP_S3_PATH_STYLE",
        "Use path-style S3 URLs? Answer true or false.",
        "text",
        default="true",
        help="Example: true",
    ),
    "KEYSTONE_SETUP_S3_BUCKET": Question(
        "KEYSTONE_SETUP_S3_BUCKET",
        "S3 bucket name for this package (for example docs-mcp).",
        "text",
        help="Example: docs-mcp",
    ),
    "KEYSTONE_SETUP_S3_ACCESS_KEY_FILE": Question(
        "KEYSTONE_SETUP_S3_ACCESS_KEY_FILE",
        "Path to a mode 600 file with the S3 access key.",
        "file",
        secret=True,
        help="Example: /home/operator/s3-access-key",
    ),
    "KEYSTONE_SETUP_S3_SECRET_KEY_FILE": Question(
        "KEYSTONE_SETUP_S3_SECRET_KEY_FILE",
        "Path to a mode 600 file with the S3 secret key.",
        "file",
        secret=True,
        help="Example: /home/operator/s3-secret-key",
    ),
    "KEYSTONE_SETUP_POSTGRES_HOST": Question(
        "KEYSTONE_SETUP_POSTGRES_HOST",
        "PostgreSQL host name (for example db.example.com).",
        "text",
        help="Example: db.example.com",
    ),
    "KEYSTONE_SETUP_POSTGRES_PORT": Question(
        "KEYSTONE_SETUP_POSTGRES_PORT",
        "PostgreSQL port.",
        "text",
        default="5432",
        help="Example: 5432",
    ),
    "KEYSTONE_SETUP_POSTGRES_SSLMODE": Question(
        "KEYSTONE_SETUP_POSTGRES_SSLMODE",
        "PostgreSQL sslmode.",
        "choice",
        choices=("disable", "prefer", "require", "verify-ca", "verify-full"),
        default="require",
        help="Example: require",
    ),
    "KEYSTONE_SETUP_POSTGRES_DATABASE": Question(
        "KEYSTONE_SETUP_POSTGRES_DATABASE",
        "PostgreSQL database name for this package.",
        "text",
        help="Example: docs_mcp",
    ),
    "KEYSTONE_SETUP_POSTGRES_USER": Question(
        "KEYSTONE_SETUP_POSTGRES_USER",
        "PostgreSQL user name for this package.",
        "text",
        help="Example: docs_mcp",
    ),
    "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE": Question(
        "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE",
        "Path to a mode 600 file with the PostgreSQL password.",
        "file",
        secret=True,
        help="Example: /home/operator/postgres-password",
    ),
    "KEYSTONE_SETUP_POSTGRES_URL_FILE": Question(
        "KEYSTONE_SETUP_POSTGRES_URL_FILE",
        "Path to a mode 600 file with a postgresql:// URL that includes sslmode. "
        "This replaces the other PostgreSQL questions.",
        "file",
        secret=True,
        help="Example: /home/operator/postgres-url",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_S3_BUCKET": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_S3_BUCKET",
        "S3 bucket for package {PKG}. Overrides KEYSTONE_SETUP_S3_BUCKET.",
        "text",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_S3_ACCESS_KEY_FILE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_S3_ACCESS_KEY_FILE",
        "Path to a mode 600 file with the S3 access key for package {PKG}.",
        "file",
        secret=True,
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_S3_SECRET_KEY_FILE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_S3_SECRET_KEY_FILE",
        "Path to a mode 600 file with the S3 secret key for package {PKG}.",
        "file",
        secret=True,
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_DATABASE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_DATABASE",
        "PostgreSQL database for package {PKG}. Overrides KEYSTONE_SETUP_POSTGRES_DATABASE.",
        "text",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_USER": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_USER",
        "PostgreSQL user for package {PKG}. Overrides KEYSTONE_SETUP_POSTGRES_USER.",
        "text",
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_PASSWORD_FILE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_PASSWORD_FILE",
        "Path to a mode 600 file with the PostgreSQL password for package {PKG}.",
        "file",
        secret=True,
    ),
    "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_URL_FILE": Question(
        "KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_URL_FILE",
        "Path to a mode 600 file with a postgresql:// URL for package {PKG}.",
        "file",
        secret=True,
    ),
}


def pkg_var(name: str, suffix: str) -> str:
    """KEYSTONE_SETUP_SERVICE_<PKG>_<SUFFIX>, with '-' in the package name as '_'."""
    pkg = name.upper().replace("-", "_")
    return f"KEYSTONE_SETUP_SERVICE_{pkg}_{suffix}"


def is_secret_var(var: str) -> bool:
    if var.endswith("_FILE"):
        return True
    template = QUESTION_TEMPLATES.get(var)
    return bool(template and template.secret)


class AnswerError(Exception):
    def __init__(self, error_id: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message
        spec = ERRORS[error_id]
        self.hint = hint if hint is not None else spec.action
        self.code = spec.code
        self.retryable = spec.retryable


def parse_sets(pairs: list[str]) -> dict[str, str]:
    """Split KEY=VALUE pairs. A secret key is refused before the value is kept."""
    found: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise AnswerError("usage", f"--set must be KEY=VALUE, got {pair!r}.")
        key, value = pair.split("=", 1)
        if not key:
            raise AnswerError("usage", "--set must be KEY=VALUE.")
        if is_secret_var(key):
            raise AnswerError(
                "secret_in_argument",
                "Refusing to accept a secret on the command line.",
            )
        found[key] = value
    return found


def read_secret_file(path: Path, secrets: SecretRegistry) -> str:
    if path.is_symlink() or not path.is_file():
        raise AnswerError(
            "secret_file_unsafe",
            "The secret file must be a regular file, not a link or a directory.",
            "chmod 600",
        )
    stat = path.stat()
    if stat.st_mode & 0o077:
        raise AnswerError(
            "secret_file_unsafe",
            "The secret file is readable by group or others.",
            "chmod 600",
        )
    if stat.st_uid not in {os.getuid(), 0}:
        raise AnswerError(
            "secret_file_unsafe",
            "The secret file must be owned by the current user or by root.",
            "chmod 600",
        )
    text = path.read_text(encoding="utf-8")
    text = text.removesuffix("\n")
    text = text.removesuffix("\r")
    secrets.register(text)
    return str(path)


def _prompt(question: Question) -> str:
    if question.help:
        print(question.help, file=sys.stderr)
    if question.kind == "choice" and question.choices:
        print(question.text, file=sys.stderr)
        for index, choice in enumerate(question.choices, start=1):
            print(f"  {index}) {choice}", file=sys.stderr)
        raw = input("choice number: ").strip()
        try:
            picked = question.choices[int(raw) - 1]
        except (ValueError, IndexError) as exc:
            raise AnswerError("usage", f"Not one of the choices for {question.var}.") from exc
        return picked
    if question.secret or question.kind == "file":
        print(question.text, file=sys.stderr)
        return input("path to secret file: ").strip()
    print(question.text, file=sys.stderr)
    return input(f"{question.var}: ").strip()


def resolve_answer(
    question: Question,
    *,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    lock_answers: Mapping[str, str],
    interactive: bool,
    secrets: SecretRegistry,
) -> str | None:
    """Return the answer, or None when it is still missing. Never returns a secret value."""
    if question.var in sets:
        if question.secret or is_secret_var(question.var):
            raise AnswerError(
                "secret_in_argument",
                "Refusing to accept a secret on the command line.",
            )
        return sets[question.var]
    env_value = environ.get(question.var)
    if env_value:
        value: str | None = env_value
    elif question.var in lock_answers and lock_answers[question.var] != "":
        value = str(lock_answers[question.var])
    elif question.default is not None:
        value = question.default
    elif interactive:
        value = _prompt(question)
    else:
        return None
    if question.secret or question.kind == "file" or is_secret_var(question.var):
        return read_secret_file(Path(value), secrets)
    return value
