"""Resource interfaces (s3/v1, postgres/v1) and package data."""

from importlib.resources import files

from keystonectl.resources.interfaces import POSTGRES_V1, S3_V1

INTERFACES = {"s3": S3_V1, "postgres": POSTGRES_V1}
RESOURCE_NETWORK = "keystone-resources"
UPSTREAMS_NETWORK = "keystone-mcp-upstreams"


def package_file(relative: str):
    """Return a Traversable for a file inside the keystonectl package."""
    return files("keystonectl").joinpath(relative)


def read_text(relative: str) -> str:
    return package_file(relative).read_text(encoding="utf-8")


def schema_text(name: str) -> str:
    return read_text(f"schemas/{name}")
