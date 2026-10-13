"""Package data loaded through importlib.resources."""

from importlib.resources import files


def package_file(relative: str):
    """Return a Traversable for a file inside the keystonectl package."""
    return files("keystonectl").joinpath(relative)


def read_text(relative: str) -> str:
    return package_file(relative).read_text(encoding="utf-8")


def schema_text(name: str) -> str:
    return read_text(f"schemas/{name}")
