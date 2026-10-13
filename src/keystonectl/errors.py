"""Error and warning registry. Later cards append their own ids."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    id: str
    code: int
    retryable: bool
    title: str
    action: str


@dataclass(frozen=True)
class WarningSpec:
    id: str
    title: str
    action: str


def _e(id: str, code: int, retryable: bool, title: str, action: str) -> tuple[str, ErrorSpec]:
    return id, ErrorSpec(id, code, retryable, title, action)


def _w(id: str, title: str, action: str) -> tuple[str, WarningSpec]:
    return id, WarningSpec(id, title, action)


ERRORS: dict[str, ErrorSpec] = dict(
    [
        _e(
            "usage",
            1,
            False,
            "The command was not understood.",
            "Check the command and flags. Run keystonectl --help.",
        ),
        _e(
            "root_not_found",
            1,
            False,
            "No deployment directory was found.",
            "Pass --root DIR, set KEYSTONECTL_ROOT, or run keystonectl init --adopt <dir>.",
        ),
        _e(
            "secret_in_argument",
            1,
            False,
            "A secret was passed on the command line.",
            "Secrets are accepted only as a path to a mode 600 file, via the environment.",
        ),
        _e(
            "not_implemented",
            1,
            False,
            "This command cannot apply changes yet.",
            "service apply has no deploy step in this build. Use service plan to preview.",
        ),
        _e(
            "package_ambiguous",
            2,
            False,
            "More than one catalog has this package.",
            "Pass the package as source/name.",
        ),
        _e(
            "keystone_mcp_version_unknown",
            2,
            False,
            "The keystone-mcp version could not be read from the image.",
            "Set KEYSTONE_SETUP_KEYSTONE_MCP_VERSION to the version that is running, "
            "for example 0.5.0.",
        ),
        _e(
            "keystone_service_ambiguous",
            2,
            False,
            "More than one compose service looks like keystone-mcp.",
            "Re-run init --adopt with --keystone-service NAME.",
        ),
        _e(
            "package_not_found",
            3,
            False,
            "The package is not in any configured catalog.",
            "Check the name, or add the catalog with keystonectl catalog add.",
        ),
        _e(
            "version_not_found",
            3,
            False,
            "That version is not listed in the catalog.",
            "Run keystonectl catalog list and pick a listed version.",
        ),
        _e(
            "source_invalid",
            3,
            False,
            "The catalog source is not valid.",
            "Use an https://, ssh://, git@host:path, or file:// URL. http:// is refused.",
        ),
        _e(
            "source_exists",
            3,
            False,
            "A source with that name or address is already configured.",
            "Pick another --name, or edit sources.yaml.",
        ),
        _e(
            "tag_not_found",
            3,
            False,
            "That git tag does not exist.",
            "The ref must be a tag or a 40-character commit. Branches are refused.",
        ),
        _e(
            "tag_moved",
            3,
            False,
            "The tag now points at a different commit than the one recorded in the lock.",
            "Refuse the move. Pin a commit, or upgrade on purpose with service upgrade.",
        ),
        _e(
            "manifest_invalid",
            3,
            False,
            "The package manifest is not valid.",
            "Fix the problems listed in result.problems and run keystonectl lint.",
        ),
        _e(
            "catalog_invalid",
            3,
            False,
            "The catalog file is not valid.",
            "Fix catalog.yaml and run keystonectl lint on it.",
        ),
        _e(
            "incompatible_keystone_mcp",
            3,
            False,
            "The package does not support this keystone-mcp version.",
            "Choose a version whose range includes the running keystone-mcp, or upgrade it.",
        ),
        _e(
            "requires_missing",
            3,
            False,
            "A required package is not installed.",
            "Install them first or use bundle install.",
        ),
        _e(
            "lock_corrupt",
            3,
            False,
            "services.lock.yaml is missing, unreadable, or not layout version 1.",
            "Restore services.lock.yaml.bak or run keystonectl init again on a copy.",
        ),
        _e(
            "secret_file_unsafe",
            3,
            False,
            "A secret file is missing, is a link, or is readable by others.",
            "Use a regular file owned by you or root and run chmod 600.",
        ),
        _e(
            "layout_unsupported",
            3,
            False,
            "This deployment uses a layout version this keystonectl does not know.",
            "layout_version must be 1.",
        ),
        _e(
            "check_failed",
            3,
            False,
            "The deployment check found an error.",
            "Read result.checks for the failing id and its hint.",
        ),
        _e(
            "source_unreachable",
            3,
            True,
            "The catalog or package source could not be reached.",
            "Check the network and the URL, then run the command again.",
        ),
        _e(
            "busy",
            3,
            True,
            "Another keystonectl process holds the lock.",
            "Wait for it to finish and run the command again.",
        ),
        _e(
            "adopt_required",
            3,
            False,
            "This directory already runs a compose project.",
            "Use keystonectl init --adopt <dir>.",
        ),
        _e(
            "root_not_empty",
            3,
            False,
            "The directory is not empty and has no compose file to adopt.",
            "Use an empty directory, or pass a directory that already runs keystone-mcp.",
        ),
        _e(
            "compose_not_found",
            3,
            False,
            "No compose file was found.",
            "Add docker-compose.yml (or compose.yaml) and run init --adopt.",
        ),
        _e(
            "compose_unparseable",
            3,
            False,
            "The compose file is not valid YAML.",
            "Fix the compose file and run init --adopt again.",
        ),
        _e(
            "keystone_service_not_found",
            3,
            False,
            "No service runs keystone-mcp.",
            "Pass --keystone-service NAME if the service was renamed.",
        ),
        _e(
            "keystone_env_file_missing",
            3,
            False,
            "The keystone-mcp service has no env file inside the deployment.",
            "Add env_file: .env to the keystone-mcp service.",
        ),
        _e(
            "path_outside_root",
            3,
            False,
            "A mounted path points outside the deployment directory.",
            "Keep registry, snapshot, and policy files inside the deployment root.",
        ),
        _e(
            "registry_path_unmapped",
            3,
            False,
            "A container path is not mounted from the deployment directory.",
            "Bind-mount the file from inside the deployment root.",
        ),
        _e(
            "registry_invalid",
            3,
            False,
            "The upstream registry is not a schema_version 1 document.",
            "The file needs schema_version: 1 and an entries list.",
        ),
        _e(
            "registry_inline",
            3,
            False,
            "Upstreams are still an inline JSON array.",
            "Move these entries to upstreams.yaml and set KEYSTONE_MCP_UPSTREAMS_PATH. "
            "keystonectl does not edit inline JSON.",
        ),
        _e(
            "env_name_conflict",
            3,
            False,
            "An environment name in the managed block is already set outside it.",
            "Remove the operator's copy of that name or rename it.",
        ),
    ]
)


WARNINGS: dict[str, WarningSpec] = dict(
    [
        _w(
            "catalog_cached",
            "The catalog source could not be updated; the cached copy was used.",
            "Restore network access and run the command again to refresh the cache.",
        ),
        _w(
            "local_source",
            "This source uses a file:// URL.",
            "file:// is for tests and local development, not for a shared catalog.",
        ),
        _w(
            "compat_mismatch",
            "The manifest compatibility range differs from the catalog.",
            "The manifest range is the one that is enforced.",
        ),
        _w(
            "catalog_ref_chosen",
            "No --ref was given, so the newest vX.Y.Z tag was selected.",
            "Pass --ref to pin a different catalog tag.",
        ),
        _w(
            "compose_file_ignored",
            "A compose file in the package is ignored.",
            "The installer never reads it.",
        ),
        _w(
            "published_name_long",
            "A published tool name is longer than 64 characters.",
            "Many MCP clients reject names longer than 64 characters.",
        ),
        _w(
            "builtin_list_may_be_stale",
            "built-in tool names are known up to keystone-mcp 0.5.0",
            "built-in tool names are known up to keystone-mcp 0.5.0; "
            "the final check is upstream validate during apply.",
        ),
        _w(
            "keystone_compose_missing",
            "No keystone-mcp compose file was found.",
            "Put keystone-mcp's docker-compose.yml and .env here "
            "(see keystone-mcp examples/production) and run keystonectl init again.",
        ),
        _w(
            "layout_changed",
            "The recognized layout differs from the lock.",
            "Review the listed fields. The lock was updated to match the compose file.",
        ),
        _w(
            "compose_variable_unset",
            "A compose variable was empty because it was not set.",
            "Set the variable in the environment or in the deployment .env file.",
        ),
        _w(
            "inline_registry_ignored",
            "KEYSTONE_MCP_UPSTREAMS is an empty JSON array and was ignored.",
            "Upstreams are read from keystonectl/upstreams.yaml.",
        ),
        _w(
            "registry_moved",
            "Existing upstream entries were copied into keystonectl/upstreams.yaml.",
            "keystone-mcp now reads keystonectl/upstreams.yaml; "
            "your old registry file is no longer read.",
        ),
        _w(
            "snapshot_moved",
            "The catalog snapshot was copied into keystonectl/catalog-snapshot.yaml.",
            "keystone-mcp now reads the snapshot from keystonectl/.",
        ),
        _w(
            "policy_not_configured",
            "No RBAC policy file was found.",
            "check will skip the policy comparison until a policy file is configured.",
        ),
        _w(
            "comments_dropped",
            "Comments in a rewritten YAML file were not kept.",
            "A one-time .orig copy was saved next to the file.",
        ),
        _w(
            "unmanaged_upstream",
            "An upstream registry entry is not managed by keystonectl.",
            "The entry is left as-is. It belongs to the operator.",
        ),
        _w(
            "orphan_fragment",
            "A package fragment has no matching lock entry.",
            "Remove the file or install the package that owns it.",
        ),
        _w(
            "fragment_modified",
            "A generated package fragment was edited by hand.",
            "Move your changes into the override file, or eject the service.",
        ),
    ]
)


def explain(error_id: str) -> ErrorSpec | WarningSpec | None:
    if error_id in ERRORS:
        return ERRORS[error_id]
    return WARNINGS.get(error_id)


def fragment_modified_message(name: str) -> str:
    return (
        f"packages/{name}.yml was edited by hand. "
        f"Move your changes to packages/{name}.override.yml, "
        f"or run `keystonectl service eject {name}` to keep the file as yours."
    )
