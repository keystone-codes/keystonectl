"""Built-in tool names copied from keystone-mcp.

Source: ``keystone_mcp.catalog.builtin.BUILTIN_TOOL_NAMES`` in keystone-mcp
0.5.0 (``src/keystone_mcp/catalog/builtin.py``). The copy is not a contract:
a newer keystone-mcp may publish more built-in tools. The installer warns
when the running core is newer than ``BUILTIN_TOOL_NAMES_VERSION``; the final
check is ``keystone-mcp upstream validate`` at install time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

BUILTIN_TOOL_NAMES_VERSION = "0.5.0"

# Same pattern and limit as keystone_mcp.upstream_registry.validate.
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")
MAX_REGISTRY_ENTRIES = 64

BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "users_list",
        "users_add",
        "users_update",
        "users_block",
        "users_unblock",
        "users_set_roles",
        "users_grant_role",
        "users_revoke_role",
        "users_logout",
        "service_accounts_list",
        "service_accounts_add",
        "service_accounts_update",
        "api_keys_issue",
        "api_keys_list",
        "api_keys_revoke",
        "policy_read",
        "policy_catalog",
        "policy_validate",
        "policy_role_set",
        "policy_role_delete",
        "policy_allow_add",
        "policy_allow_remove",
        "policy_list_set",
        "policy_rollback",
    }
)


def reserved_namespace_errors(ns: str) -> list[str]:
    """Return manifest errors when ``ns_`` would collide with a built-in tool.

    Same rule as ``keystone_mcp.upstream_registry.validate.namespace_errors``:
    a namespace is reserved when ``namespace + "_"`` is a prefix of a built-in
    tool name. Pairwise namespace collisions are an install-time check.
    """
    prefix = f"{ns}_"
    errors: list[str] = []
    for tool in sorted(BUILTIN_TOOL_NAMES):
        if tool.startswith(prefix):
            errors.append(
                f"mcp.namespace: '{ns}' is reserved: published names {ns}_* "
                f"can collide with keystone-mcp built-in tool {tool}"
            )
    return errors


def namespace_errors(namespaces: Sequence[str]) -> list[str]:
    """Registry-wide namespace checks, copied from keystone-mcp 0.5.0.

    Same three rules as ``keystone_mcp.upstream_registry.validate.namespace_errors``:
    the core pattern, collisions with built-in tool names, and pairs where one
    namespace starts with the other plus ``_``.
    """
    errors: list[str] = []
    for ns in namespaces:
        if not _NAMESPACE_RE.match(ns):
            errors.append(
                f"namespace {ns!r} must be 1-64 characters of A-Z, a-z, 0-9, "
                "'_' or '-', not starting or ending with '_' or '-'"
            )
    for ns in namespaces:
        prefix = f"{ns}_"
        for builtin_name in sorted(BUILTIN_TOOL_NAMES):
            if builtin_name.startswith(prefix):
                errors.append(
                    f"namespace {ns!r} is reserved: published names {ns}_* can "
                    f"collide with built-in tool {builtin_name}"
                )
    distinct = sorted(set(namespaces))
    for left in distinct:
        for right in distinct:
            if left == right:
                continue
            if right.startswith(f"{left}_"):
                errors.append(
                    f"namespaces {left!r} and {right!r} can publish the same tool name "
                    f"({left}_x_y == {right}_y when {left} has a tool x_y); rename one of them"
                )
    return errors
