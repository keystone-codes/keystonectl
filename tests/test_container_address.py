"""Compose container names are <project>-<service>-<n>, not the service name."""

from __future__ import annotations

import json
from types import SimpleNamespace

from keystonectl.generate.install import ComposeDeployer
from keystonectl.resources.network import ip_for_service
from keystonectl.runner import CommandResult


def test_compose_replica_name_resolves_to_the_service_ip() -> None:
    containers = {
        "abc": {
            "Name": "keystone-production-minio-server-1",
            "IPv4Address": "172.18.0.4/16",
        }
    }
    assert ip_for_service(containers, "minio-server") == "172.18.0.4"
    assert ip_for_service(containers, "minio") == ""


def test_alias_still_resolves() -> None:
    containers = {"abc": {"Name": "other", "IPv4Address": "10.0.0.8/16", "Aliases": ["minio"]}}
    assert ip_for_service(containers, "minio") == "10.0.0.8"


def test_deployer_uses_the_compose_container_name() -> None:
    payload = {
        "abc": {
            "Name": "keystone-production-minio-server-1",
            "IPv4Address": "172.18.0.4/16",
        }
    }

    class Runner:
        def run(self, args, **_kwargs):
            return CommandResult(tuple(args), 0, json.dumps(payload), "")

    ip = ComposeDeployer().container_ip(
        SimpleNamespace(runner=Runner()), "minio-server", "keystone-resources"
    )
    assert ip == "172.18.0.4"
