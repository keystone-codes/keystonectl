"""Reset extension points between tests."""

import pytest

from keystonectl.check import reset_checks
from keystonectl.plan import reset_steps


@pytest.fixture(autouse=True)
def _reset_extensions() -> None:
    reset_steps()
    reset_checks()
    from keystonectl.resources.binding import register_resources

    register_resources()
    yield
    reset_steps()
    reset_checks()
