"""Package reference syntax [source/]name[@version]."""

from __future__ import annotations

import pytest

from keystonectl.catalog import PackageRef, parse_package_ref


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("file-mcp", PackageRef(None, "file-mcp", None)),
        ("file-mcp@1.0.0", PackageRef(None, "file-mcp", "1.0.0")),
        ("keystone-codes/file-mcp@1.0.0", PackageRef("keystone-codes", "file-mcp", "1.0.0")),
    ],
)
def test_parse_package_ref(text: str, expected: PackageRef) -> None:
    assert parse_package_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    ["a/b/c", "file-mcp@latest", "File-mcp", "/file-mcp"],
)
def test_parse_package_ref_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="package reference must look like"):
        parse_package_ref(text)
