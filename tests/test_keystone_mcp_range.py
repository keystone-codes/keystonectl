"""keystone-mcp compatibility range grammar."""

from __future__ import annotations

import pytest

from keystonectl.catalog import is_compatible, keystone_mcp_range_errors
from keystonectl.manifest import manifest_errors


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        (">=0.5.0,<2.0", True),
        (">=0.5.0,<2.0,!=0.5.3", True),
        (">=0.5", False),
        ("<2.0", False),
        ("~=0.5", False),
        ("==0.5.0", False),
        (">=2.0,<1.0", False),
        ("garbage", False),
    ],
)
def test_range_errors(spec: str, ok: bool) -> None:
    errors = keystone_mcp_range_errors(spec)
    if ok:
        assert errors == []
    else:
        assert errors


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.5.0", True),
        ("0.4.9", False),
        ("2.0.0", False),
        ("0.5.1rc1", True),
        ("2.0.0rc1", False),
    ],
)
def test_is_compatible(version: str, expected: bool) -> None:
    assert is_compatible(">=0.5.0,<2.0", version) is expected


def test_manifest_compat_range_checked() -> None:
    document = {
        "schema_version": 1,
        "kind": "service",
        "name": "demo",
        "version": "1.0.0",
        "publisher": "acme",
        "summary": "Demo",
        "compat": {"keystone_mcp": "~=0.5", "contracts": []},
        "containers": {
            "app": {
                "image": "ghcr.io/example/demo:1.0.0@sha256:" + "a" * 64,
                "port": 8080,
                "healthcheck": {"path": "/healthz"},
            }
        },
    }
    errors = manifest_errors(document)
    assert (
        "compat.keystone_mcp: keystone_mcp range '~=0.5': operator '~=' is not allowed; "
        "use >=, < and !="
    ) in errors
