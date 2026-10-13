"""Register the service deploy step, compose checks, and ``service status``."""

from __future__ import annotations

import argparse


def ensure_registered() -> None:
    """Register resources, deploy, and compose checks. Safe to call more than once."""

    from keystonectl.generate.checks import ensure_checks
    from keystonectl.generate.install import DeployStep
    from keystonectl.plan import _EXTRAS, register_step
    from keystonectl.resources.binding import register_resources

    register_resources()
    if not any(getattr(step, "id", None) == "deploy" for step, _after in _EXTRAS):
        register_step(DeployStep(), after="resources")
    ensure_checks()


def register(subparsers: argparse._SubParsersAction) -> None:
    status = subparsers.add_parser("status", help="Show package container and gateway status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--root", default=None)
    status.add_argument("--non-interactive", action="store_true")
    status.add_argument("--set", action="append", default=None)
    status.add_argument("--version", action="store_true")
    status.add_argument("name", nargs="?")
