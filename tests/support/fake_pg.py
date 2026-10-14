"""Fake psycopg connection for postgres_probe unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql


def _sql_text(query: Any) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, sql.Composed):
        chunks: list[str] = []
        for part in query:
            if isinstance(part, sql.SQL):
                chunks.append(part._obj)
            elif isinstance(part, sql.Identifier):
                chunks.append(part._obj[0])
            else:
                chunks.append(str(part))
        return "".join(chunks)
    return str(query)


@dataclass
class FakePgCursor:
    responses: dict[str, list[tuple[Any, ...] | Exception]]
    rollback: Callable[[], None]
    _pending: list[tuple[Any, ...]] = field(default_factory=list)
    _last_key: str = ""

    def execute(self, query: Any, params: Any = None) -> None:
        text = _sql_text(query).strip()
        key = text.split("(", maxsplit=1)[0].strip().lower()
        if key.startswith("create table"):
            key = "create table"
        elif key.startswith("insert into"):
            key = "insert"
        elif key.startswith("select id from"):
            key = "select probe"
        entry = self.responses.get(text) or self.responses.get(key)
        if entry is None:
            for prefix, value in self.responses.items():
                if text.lower().startswith(prefix.lower()):
                    entry = value
                    break
        if entry is None:
            self._pending = []
            return
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, Iterable) and not isinstance(entry, (str, bytes)):
            rows = list(entry)
            if rows and isinstance(rows[0], Exception):
                raise rows[0]
            self._pending = rows
        else:
            self._pending = []

    def fetchone(self) -> tuple[Any, ...] | None:
        if not self._pending:
            return None
        return self._pending.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._pending
        self._pending = []
        return rows

    def __enter__(self) -> FakePgCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@dataclass
class FakePgConnection:
    responses: dict[str, list[tuple[Any, ...] | Exception]]
    connect_kwargs: dict[str, Any] = field(default_factory=dict)
    autocommit: bool = False
    rollback_calls: int = 0
    commit_calls: int = 0
    closed: bool = False
    # Recorded after construction: every value the probe assigns to ``.autocommit``.
    # psycopg 3 raises if autocommit is toggled while a transaction is open (INTRANS),
    # so the probe must never touch this attribute after connecting.
    autocommit_assignments: list[bool] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.autocommit_assignments = []

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "autocommit" and "autocommit_assignments" in self.__dict__:
            self.autocommit_assignments.append(value)
        super().__setattr__(name, value)

    def cursor(self) -> FakePgCursor:
        return FakePgCursor(
            responses=self.responses,
            rollback=self.rollback,
        )

    def rollback(self) -> None:
        self.rollback_calls += 1

    def commit(self) -> None:
        self.commit_calls += 1

    def close(self) -> None:
        self.closed = True


def make_connect_factory(
    responses: dict[str, list[tuple[Any, ...] | Exception]],
    *,
    connect_error: BaseException | None = None,
) -> Callable[..., FakePgConnection]:
    captured: dict[str, Any] = {"kwargs": {}}

    def factory(**kwargs: Any) -> FakePgConnection:
        captured["kwargs"] = dict(kwargs)
        if connect_error is not None:
            raise connect_error
        return FakePgConnection(responses=responses, connect_kwargs=dict(kwargs))

    factory.captured = captured  # type: ignore[attr-defined]
    return factory


def ok_responses(
    *,
    server_version_num: int = 160004,
    schema: str = "public",
    extensions: list[str] | None = None,
    rolsuper: bool = False,
    other_databases: list[str] | None = None,
) -> dict[str, list[tuple[Any, ...] | Exception]]:
    ext_rows = [(name,) for name in (extensions or [])]
    other_rows = [(name,) for name in (other_databases or [])]
    return {
        "select version()": [("PostgreSQL 16.4",)],
        "show server_version_num": [(str(server_version_num),)],
        "select current_schema()": [(schema,)],
        "create table": [()],
        "insert": [()],
        "select probe": [(1,)],
        "select extname from pg_extension": ext_rows,
        "select rolsuper, rolcreatedb, rolcreaterole": [(rolsuper, False, False)],
        "select datname from pg_database where datallowconn": other_rows,
    }


def auth_error_sqlstate() -> psycopg.Error:
    return psycopg.errors.lookup("28P01")("password authentication failed")


def permission_denied() -> psycopg.Error:
    return psycopg.errors.lookup("42501")("permission denied for schema public")
