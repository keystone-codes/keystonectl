"""Error and warning registry. Later cards append their own ids."""

from dataclasses import dataclass


class KsctlError(Exception):
    """Installer error that carries a registry id."""

    def __init__(
        self,
        error_id: str,
        message: str,
        *,
        retryable: bool = False,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message
        self.retryable = retryable
        spec = ERRORS.get(error_id)
        self.hint = hint if hint is not None else (spec.action if spec is not None else "")
        self.code = spec.code if spec is not None else 3


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
            "No deploy step is configured for this change. Run service plan to preview it.",
        ),
        _e(
            "package_ambiguous",
            2,
            False,
            "More than one catalog has this package.",
            "Pass the package as source/name, for example local/file-mcp. "
            "service upgrade reads the source from the lock.",
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
            "source_not_found",
            3,
            False,
            "No source with that name is configured.",
            "Run keystonectl catalog list and pass a name from that list.",
        ),
        _e(
            "tag_not_found",
            3,
            False,
            "That git tag does not exist.",
            "Check the ref in sources.yaml or catalog.yaml. For the default source, "
            "update keystonectl or add sources.yaml with a ref that exists.",
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
            "provider_invalid",
            3,
            False,
            "The stdio provider descriptor is not valid.",
            "Fix the problems listed in result.problems and run "
            "keystonectl package wrap-stdio again.",
        ),
        _e(
            "discovery_failed",
            3,
            True,
            "The server did not list its tools.",
            "Read the lines in result.stderr; if the server needs a real account to start, "
            "set discovery.mode: manual and pass --tools-snapshot.",
        ),
        _e(
            "manifest_unrecognized",
            3,
            False,
            "The existing manifest could not be updated in place.",
            "Run again with --force to rewrite it.",
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
        _e(
            "no_provider",
            3,
            False,
            "No catalog provider offers this resource.",
            "Choose external and point at storage you already run, or add a catalog that provides it.",
        ),
        _e(
            "resource_already_bound",
            3,
            False,
            "This installation already has a provider for that resource.",
            "One provider per kind. Use the provider that is already bound, or remove it first.",
        ),
        _e(
            "resource_in_use",
            3,
            False,
            "That provider still has consumers.",
            "Unbind the consumers first. Their buckets and databases are kept.",
        ),
        _e(
            "s3_access_denied",
            3,
            False,
            "S3 refused the access key.",
            "Check the access key and the secret key, then run the command again.",
        ),
        _e(
            "s3_bucket_missing",
            3,
            False,
            "The S3 bucket does not exist.",
            "Create the bucket in the storage you already have, then run the command again.",
        ),
        _e(
            "s3_wrong_region",
            3,
            False,
            "The S3 region does not match the bucket.",
            "Set KEYSTONE_SETUP_S3_REGION to the bucket's region.",
        ),
        _e(
            "s3_presign_failed",
            3,
            False,
            "S3 could not sign an upload or download link.",
            "Allow s3:GetObject and s3:PutObject on the bucket, then run the command again.",
        ),
        _e(
            "postgres_access_denied",
            3,
            False,
            "PostgreSQL refused the user or password.",
            "Check the user and the password file, then run the command again.",
        ),
        _e(
            "postgres_database_missing",
            3,
            False,
            "The PostgreSQL database does not exist.",
            "Create the database and the user, then run the command again.",
        ),
        _e(
            "postgres_version_too_old",
            3,
            False,
            "PostgreSQL is older than version 14.",
            "Use a PostgreSQL server that is version 14 or newer.",
        ),
        _e(
            "postgres_create_table_failed",
            3,
            False,
            "The PostgreSQL user cannot create tables in its database.",
            "Grant the user rights on its own schema, then run the command again.",
        ),
        _e(
            "postgres_not_isolated",
            3,
            False,
            "The PostgreSQL user can reach another database.",
            "This is a provider bug. The consumer must not connect to other databases.",
        ),
        _e(
            "postgres_extension_missing",
            3,
            False,
            "A required PostgreSQL extension is missing.",
            "Create the extensions named in the message, then run the command again.",
        ),
        _e(
            "postgres_url_invalid",
            3,
            False,
            "The PostgreSQL URL is missing sslmode.",
            "Add sslmode to the URL, for example ?sslmode=require.",
        ),
        _e(
            "postgres_address_mismatch",
            3,
            False,
            "The PostgreSQL URL points at a different server than the one already bound.",
            "Use the host, port, and sslmode already recorded for PostgreSQL. "
            "Database, user, and password can differ for each service.",
        ),
        _e(
            "s3_unreachable",
            3,
            True,
            "S3 could not be reached.",
            "Check the endpoint and the network, then run the command again.",
        ),
        _e(
            "postgres_unreachable",
            3,
            True,
            "PostgreSQL could not be reached.",
            "Check the host, the port, and the network, then run the command again.",
        ),
        _e(
            "provision_failed",
            3,
            False,
            "The provider could not create the consumer's access.",
            "If the provider was not ready yet, run the command again. "
            "Otherwise fix the provider package.",
        ),
        _e(
            "docker_unavailable",
            3,
            True,
            "Docker or the keystone-resources network is not available.",
            "Start Docker. The keystone-resources network is created when a service is deployed.",
        ),
        _e(
            "registry_name_taken",
            3,
            False,
            "That registry name is already used by an entry keystonectl does not own.",
            "Pick another package name, or remove the hand-written registry entry.",
        ),
        _e(
            "registry_full",
            3,
            False,
            "The upstream registry already has 64 entries.",
            "Remove an entry that keystonectl does not manage, then run the command again.",
        ),
        _e(
            "namespace_change_unsupported",
            3,
            False,
            "The tool prefix of an installed service cannot be changed.",
            "Run keystonectl service remove, then install the service again.",
        ),
        _e(
            "policy_breaks",
            3,
            False,
            "The catalog snapshot would make the current policy invalid.",
            "Fix the policy, or install a version whose tools the policy already allows.",
        ),
        _e(
            "compose_too_old",
            3,
            False,
            "Docker Compose is older than 2.24.",
            "Upgrade the Docker Compose plugin to 2.24 or newer.",
        ),
        _e(
            "layout_invalid",
            3,
            False,
            "keystone-mcp cannot see the registry, the snapshot, or the service network.",
            "Run keystonectl init --adopt <root> and start compose with both files: "
            "docker compose -f docker-compose.yml -f docker-compose.packages.yml.",
        ),
        _e(
            "fragment_modified",
            3,
            False,
            "A generated package fragment was edited by hand.",
            "Move your changes into the override file, or eject the service.",
        ),
        _e(
            "init_failed",
            3,
            False,
            "An init container exited with an error.",
            "Read the container log, fix the package, and run the command again.",
        ),
        _e(
            "keystone_mcp_image_unavailable",
            3,
            True,
            "The keystone-mcp image is not available.",
            "Pull the image recorded in the lock, or build it with your deployment's "
            "own tooling, then retry.",
        ),
        _e(
            "service_unhealthy",
            3,
            True,
            "A service container did not become healthy.",
            "Read the container log and run the command again.",
        ),
        _e(
            "domain_invalid",
            3,
            False,
            "The public domain is not a lowercase hostname.",
            "Use a lowercase DNS name with at least two labels, for example files.example.com.",
        ),
        _e(
            "domain_in_use",
            3,
            False,
            "That domain is already used by another service or nginx site.",
            "Pick another domain, or disable the other site and run the command again.",
        ),
        _e(
            "sudo_required",
            3,
            False,
            "nginx and certificates need passwordless sudo, or TLS=manual.",
            "Allow sudo -n for nginx and certbot, or answer TLS=manual.",
        ),
        _e(
            "nginx_missing",
            3,
            False,
            "nginx is not installed.",
            "Install nginx, or answer TLS=manual.",
        ),
        _e(
            "nginx_config_invalid",
            3,
            False,
            "nginx -t rejected the new site.",
            "Read the nginx -t output. The previous site file was restored.",
        ),
        _e(
            "certbot_missing",
            3,
            False,
            "certbot is not installed.",
            "Install certbot and python3-certbot-nginx, or choose another TLS method.",
        ),
        _e(
            "cert_unreadable",
            3,
            False,
            "The certificate or key file cannot be read.",
            "Check the path and try another KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH or _KEY_PATH.",
        ),
        _e(
            "cert_not_pem",
            3,
            False,
            "The certificate is not a PEM file.",
            "A .pfx or .der file must be converted: "
            "openssl pkcs12 -in x.pfx -nokeys -out fullchain.pem",
        ),
        _e(
            "key_not_pem",
            3,
            False,
            "The private key is not a PEM file.",
            "Replace KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH with a PEM key.",
        ),
        _e(
            "key_mismatch",
            3,
            False,
            "The private key does not match the certificate.",
            "Point KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH at the key for this certificate.",
        ),
        _e(
            "cert_expired",
            3,
            False,
            "The certificate has expired.",
            "Replace KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH with a current certificate.",
        ),
        _e(
            "cert_install_failed",
            3,
            False,
            "The certificate files could not be installed.",
            "Check sudo and the destination directory, then run the command again.",
        ),
        _e(
            "port_range_exhausted",
            3,
            False,
            "Every loopback port from 47600 to 47699 is in use.",
            "Free a port in that range and run the command again.",
        ),
        _e(
            "required_by",
            3,
            False,
            "Another installed service requires this one.",
            "Remove the services listed in the message first.",
        ),
        _e(
            "no_previous_version",
            3,
            False,
            "There is no previous version to roll back to.",
            "Rollback restores one upgrade. Install the version you want with service apply.",
        ),
        _e(
            "not_ejectable",
            3,
            False,
            "A remote service has no generated fragment to eject.",
            "Remote services are not generated. There is nothing to eject.",
        ),
        _e(
            "not_ejected",
            3,
            False,
            "This service was not ejected.",
            "service adopt only takes back a fragment that was ejected.",
        ),
        _e(
            "bundle_not_found",
            3,
            False,
            "The bundle is not in any configured catalog.",
            "Check the name, or add the catalog with keystonectl catalog add.",
        ),
        _e(
            "requires_cycle",
            3,
            False,
            "The bundle's requires dependencies contain a cycle.",
            "Remove the cycle in the package manifests and run the command again.",
        ),
        _e(
            "remote_url_mismatch",
            3,
            False,
            "The URL does not match the registry entry being adopted.",
            "Pass the URL already stored in keystonectl/upstreams.yaml.",
        ),
        _e(
            "not_package",
            3,
            False,
            "A remote service has no package.",
            "A remote service has no package; use add-remote to change its url.",
        ),
        _e(
            "priority_taken",
            3,
            False,
            "That source priority is already used.",
            "Pick another --priority. Priority only affects listing order.",
        ),
        _e(
            "public_ip_unknown",
            3,
            True,
            "The public address of this host could not be read.",
            "Check outbound HTTPS and run the command again, or answer DNS=skip.",
        ),
        _e(
            "dns_no_record",
            3,
            True,
            "The domain has no A record.",
            "Add an A record for the domain pointing at this host, then run apply again.",
        ),
        _e(
            "dns_mismatch",
            3,
            True,
            "The domain does not point at this host.",
            "Update the A record or wait for DNS to propagate (often 1-10 min), "
            "then run apply again.",
        ),
        _e(
            "certbot_failed",
            3,
            True,
            "certbot could not issue the certificate.",
            "Check DNS, port 80, and /var/log/letsencrypt/letsencrypt.log, then run apply again.",
        ),
        _e(
            "public_unreachable",
            3,
            True,
            "https on the public domain did not answer.",
            "Finish the proxy and certificate, then run apply again.",
        ),
        _e(
            "resource_not_bound",
            3,
            False,
            "This service is not bound to that resource.",
            "Pass a kind this service lists, s3 or postgres.",
        ),
        _e(
            "rotate_external_unsupported",
            3,
            False,
            "An external resource secret cannot be rotated from here.",
            "Remove the service and install it again.",
        ),
        _e(
            "rotate_failed",
            3,
            True,
            "The secret could not be rotated.",
            "Run the command again.",
        ),
        _e(
            "internal_error",
            3,
            False,
            "An unexpected error occurred.",
            "Report this with the command and the keystonectl version.",
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
            "builtin_tools_unavailable",
            "The live built-in tool list could not be read during apply.",
            "Check that keystone-mcp is healthy and its version is supported (see `keystonectl check`), "
            "then run apply again.",
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
        _w(
            "s3_plain_http",
            "The S3 endpoint uses plain HTTP.",
            "Prefer https:// for storage that is not on your own Docker network.",
        ),
        _w(
            "postgres_plain_connection",
            "The PostgreSQL connection does not require certificate verification.",
            "Prefer sslmode require, verify-ca, or verify-full for a server you do not run here.",
        ),
        _w(
            "s3_lifecycle_unsupported",
            "Storage works, but old uploads will not be deleted automatically.",
            "The service still runs. Add a lifecycle rule on the bucket later if you need it.",
        ),
        _w(
            "resource_answer_ignored",
            "An answer about a resource that is already bound was ignored.",
            "The installation already has one provider for this kind; every service uses it. "
            "To move a service elsewhere, remove it and the provider first.",
        ),
        _w(
            "provider_unused",
            "A catalog resource provider has no consumers left.",
            "Remove the provider with service remove when you no longer need its data on disk.",
        ),
        _w(
            "provider_data_kept",
            "Provider volumes still hold every former consumer's data.",
            "Back up first, then delete volumes with docker volume rm if you will not reinstall.",
        ),
        _w(
            "policy_check_skipped",
            "The policy check was skipped.",
            "Set KEYSTONE_MCP_RBAC_MODE to something other than off and point "
            "KEYSTONE_MCP_RBAC_POLICY_PATH at a policy file.",
        ),
        _w(
            "policy_already_inconsistent",
            "The policy already has the same unmatched allow patterns.",
            "The new snapshot does not add unmatched patterns. Fix the policy when you can.",
        ),
        _w(
            "tools_unreachable",
            "No role can call these tools yet.",
            "Ask an administrator to add policy rows (see the recommended policy below).",
        ),
        _w(
            "discovery_pending",
            "keystone-mcp has not discovered this service yet.",
            "keystone-mcp will try again on its own. Check keystonectl service status.",
        ),
        _w(
            "validation_skipped_local",
            "Upstream and policy checks were skipped because the local image is not built.",
            "Build the keystone-mcp image, then run keystonectl service apply again.",
        ),
        _w(
            "override_not_included",
            "An override file exists but is not included.",
            "Run keystonectl service apply <name> to include it.",
        ),
        _w(
            "override_widens_access",
            "The merged compose model gives a package service more access than the manifest.",
            "This is the operator's choice. Review ports, privileges, and host mounts.",
        ),
        _w(
            "override_unchecked",
            "Override access could not be checked.",
            "Fix the compose files and run keystonectl check again.",
        ),
        _w(
            "compose_version_unknown",
            "Docker Compose version could not be read.",
            "Install the Docker Compose plugin 2.24 or newer.",
        ),
        _w(
            "keystone_wiring_stale",
            "keystone-mcp is running without every compose file or the upstreams network.",
            "Recreate it with all compose files, for example "
            "`docker compose -f docker-compose.yml -f docker-compose.packages.yml "
            "up -d --force-recreate <keystone service>`.",
        ),
        _w(
            "check_skipped_no_docker",
            "A check that needs Docker was skipped.",
            "Install Docker to run this check.",
        ),
        _w(
            "unparsed_enddate",
            "The certificate end date could not be read.",
            "Check the certificate file format and run keystonectl check again.",
        ),
        _w(
            "tls_untrusted",
            "The public certificate is not trusted by the system trust store.",
            "The service answered. Install the certificate in the system trust store "
            "if clients should trust it.",
        ),
        _w(
            "dns_system_resolver",
            "dig is not installed, so the system resolver was used.",
            "Install dig for a check against 1.1.1.1. The system resolver can be cached.",
        ),
        _w(
            "chain_missing",
            "The certificate file has no chain.",
            "Add the intermediate certificates to the full chain.",
        ),
        _w(
            "cert_may_not_cover_domain",
            "The certificate may not cover this domain.",
            "Check the certificate names. A wildcard covers one label only.",
        ),
        _w(
            "cert_expiring",
            "The certificate expires soon.",
            "Renew it before it expires. service status warns again until you do.",
        ),
        _w(
            "renewal_timer_missing",
            "certbot has no renewal timer.",
            "Enable the certbot systemd timer or /etc/cron.d/certbot.",
        ),
        _w(
            "renewal_manual",
            "Renewal of this certificate is on you.",
            "service status warns 30 days before expiry.",
        ),
        _w(
            "public_check_failed",
            "The public health check did not return 200.",
            "The service is installed. Check the proxy and run apply again.",
        ),
        _w(
            "public_pending",
            "The public address is not ready.",
            "Run service apply again with a domain when you want the public paths published.",
        ),
        _w(
            "extra_hosts_manual",
            "A fragment was not updated with extra_hosts.",
            "Add the host line yourself in the override or in your own compose file.",
        ),
        _w(
            "policy_rows_orphaned",
            "Policy rows still match tools this service no longer publishes.",
            "Delete these policy rows. The tool names stay reserved until you do.",
        ),
        _w(
            "generated_secrets_kept",
            "Generated secrets of a package with volumes are kept so a later install can reuse them.",
            "Delete .keystonectl/secrets/<name>.generated.env only together with the volumes "
            "that still need those secrets.",
        ),
        _w(
            "remote_without_identity",
            "This remote service has no bearer or identity secret.",
            "Catalog packages must check the assertion. Here that check is yours.",
        ),
        _w(
            "remote_tools_not_in_snapshot",
            "Live tools are missing from the catalog snapshot.",
            "Run keystonectl service add-remote again with --tools.",
        ),
        _w(
            "remote_tools_unknown",
            "Policy rows for this namespace are checked only while the service is up.",
            "Start the service on the keystone-mcp-upstreams network and run check again.",
        ),
        _w(
            "remote_unreachable",
            "The remote URL did not answer from the keystone-mcp-upstreams network.",
            "keystone-mcp will keep retrying; make sure the service is on the "
            "keystone-mcp-upstreams network.",
        ),
        _w(
            "package_name_shadowed",
            "A package name now exists in more than one catalog.",
            "Install it by full name source/name.",
        ),
        _w(
            "bundle_name_shadowed",
            "A bundle name now exists in more than one catalog.",
            "Install it by full name source/name.",
        ),
        _w(
            "default_catalog_behind",
            "A catalog source is pinned to an older tag than this keystonectl expects.",
            "Run keystonectl catalog set-ref with the tag named in the warning.",
        ),
        _w(
            "installed_version_missing",
            "An installed package version is no longer listed in its catalog.",
            "Upgrade or apply will need another version of that package.",
        ),
        _w(
            "tools_snapshot_missing",
            "The package lists only the creds_* tools.",
            "Run wrap-stdio again without --no-discovery.",
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
