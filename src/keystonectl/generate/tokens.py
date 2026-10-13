"""Secret tokens. Tests replace ``token_urlsafe`` with a stand-in."""

from __future__ import annotations

import secrets


def token_urlsafe(nbytes: int) -> str:
    return secrets.token_urlsafe(nbytes)
