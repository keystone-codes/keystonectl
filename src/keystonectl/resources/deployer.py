"""Deployer protocol. I03 implements it; this card only calls it."""

from __future__ import annotations

from typing import Literal, Protocol

from keystonectl.plan import Context
from keystonectl.sources import ResolvedPackage


class Deployer(Protocol):
    def deploy(
        self,
        ctx: Context,
        package: ResolvedPackage,
        *,
        role: Literal["service", "resource"],
    ) -> None: ...

    def wait_healthy(self, ctx: Context, name: str, *, timeout: float) -> None: ...

    def container_ip(self, ctx: Context, service: str, network: str) -> str: ...
