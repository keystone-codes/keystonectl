"""Apply a package manifest through the real pipeline with a fake Docker."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from keystonectl.output import SecretRegistry
from keystonectl.plan import run_service
from keystonectl.runner import CommandResult, FakeCommandRunner
from tests.support import SHA, write_empty_lock

CLOCK = datetime(2026, 10, 13, tzinfo=UTC)
IMAGE = "keystone-mcp:0.5.0"

LAYOUT = {
    "flavor": "custom",
    "project": "demo",
    "compose_files": ["docker-compose.yml", "docker-compose.packages.yml"],
    "keystone_service": "keystone",
    "profiles": [],
    "keystone_image": IMAGE,
    "keystone_build": None,
    "keystone_mcp_version": "0.5.0",
    "env_file": ".env",
    "policy": None,
    "upstreams_network": {
        "name": "keystone-mcp-upstreams",
        "attached_by": "keystonectl",
    },
}

CONFIG = {
    "name": "demo",
    "services": {
        "keystone": {
            "image": IMAGE,
            "environment": {
                "KEYSTONE_MCP_UPSTREAMS_PATH": "/etc/keystone-mcp/keystonectl/upstreams.yaml",
                "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT": "/etc/keystone-mcp/keystonectl/catalog-snapshot.yaml",
            },
            "volumes": ["./keystonectl:/etc/keystone-mcp/keystonectl:ro"],
            "networks": ["keystone-mcp-upstreams"],
        }
    },
}


class DockerBox:
    """Canned answers for every ``docker`` invocation during apply."""

    def __init__(self) -> None:
        self.version = "2.29.1\n"
        self.config = dict(CONFIG)
        self.validate_code = 0
        self.validate_stderr = ""
        self.catalog_code = 0
        self.catalog_stderr = ""
        self.catalog_script: list[tuple[int, str]] | None = None
        self.image_ok = True
        self.daemon = False
        self.keystone_id = ""
        self.metrics = ""
        self.logs = ""
        self.live_names = ""
        self.init_fail: set[str] = set()
        self.started: list[str] = []
        self.inspect_health = "healthy"

    def __call__(self, args, **_kwargs) -> CommandResult:
        if self.daemon:
            return CommandResult(tuple(args), 1, "", "Cannot connect to the Docker daemon")
        if args[:4] == ["docker", "compose", "version", "--short"] or (
            len(args) >= 4 and args[1] == "compose" and "version" in args
        ):
            return CommandResult(tuple(args), 0, self.version, "")
        if args[0] == "docker" and args[1] == "compose" and "config" in args:
            return CommandResult(tuple(args), 0, json.dumps(self.config), "")
        if args[:3] == ["docker", "image", "inspect"]:
            code = 0 if self.image_ok else 1
            return CommandResult(tuple(args), code, "[]" if code == 0 else "", "")
        if args[:3] == ["docker", "image", "pull"] or (args[0] == "docker" and args[1] == "pull"):
            return CommandResult(tuple(args), 0 if self.image_ok else 1, "", "")
        if args[0] == "docker" and args[1] == "network":
            return CommandResult(tuple(args), 0, "{}", "")
        if args[0] == "docker" and args[1] == "inspect":
            service = args[-1]
            if service in self.init_fail:
                state = {"Status": "exited", "ExitCode": 1, "Health": {"Status": ""}}
            elif service.endswith("-migrate"):
                state = {"Status": "exited", "ExitCode": 0, "Health": {"Status": ""}}
            else:
                state = {
                    "Status": "running",
                    "ExitCode": 0,
                    "Health": {"Status": self.inspect_health},
                }
            return CommandResult(tuple(args), 0, json.dumps([{"State": state}]), "")
        if args[0] == "docker" and args[1] == "run":
            if "catalog" in args and "validate" in args:
                if self.catalog_script:
                    code, err = self.catalog_script.pop(0)
                    return CommandResult(tuple(args), code, "", err)
                return CommandResult(tuple(args), self.catalog_code, "", self.catalog_stderr)
            return CommandResult(tuple(args), self.validate_code, "", self.validate_stderr)
        if args[0] == "docker" and args[1] == "info":
            return CommandResult(tuple(args), 0, "ok\n", "")
        if args[0] == "docker" and args[1] == "compose" and "ps" in args:
            if "--status" in args:
                return CommandResult(tuple(args), 0, self.keystone_id, "")
            lines = []
            names = list(self.started)
            for service in names:
                if service in self.init_fail:
                    row = {
                        "ID": service,
                        "Service": service,
                        "State": "exited",
                        "ExitCode": 1,
                        "Health": "",
                    }
                elif service.endswith("-migrate"):
                    row = {
                        "ID": service,
                        "Service": service,
                        "State": "exited",
                        "ExitCode": 0,
                        "Health": "",
                    }
                else:
                    row = {
                        "ID": service,
                        "Service": service,
                        "State": "running",
                        "ExitCode": 0,
                        "Health": "healthy",
                    }
                lines.append(json.dumps(row))
            return CommandResult(tuple(args), 0, "\n".join(lines) + ("\n" if lines else ""), "")
        if args[0] == "docker" and args[1] == "compose" and "exec" in args:
            if "keystone_mcp.rbac.catalog" in args:
                return CommandResult(tuple(args), 0, self.live_names, "")
            if "healthcheck" in args:
                return CommandResult(tuple(args), 0, "", "")
            if "metrics" in " ".join(args):
                return CommandResult(tuple(args), 0, self.metrics, "")
            return CommandResult(tuple(args), 0, self.logs, "")
        if args[0] == "docker" and args[1] == "compose" and "logs" in args:
            return CommandResult(tuple(args), 0, self.logs, "")
        if args[0] == "docker" and args[1] == "compose" and "up" in args:
            names = [item for item in args if item.startswith(tuple(self._prefixes()))]
            if names:
                self.started = names
            return CommandResult(tuple(args), 0, "", "")
        if args[0] == "docker" and args[1] == "compose":
            return CommandResult(tuple(args), 0, "", "")
        return CommandResult(tuple(args), 0, "", "")

    def _prefixes(self) -> tuple[str, ...]:
        return ("file-mcp-", "docs-service-", "hello-worker-", "minio-", "facade-")


def prepare_root(root: Path, manifests: dict[str, str], *, resources: list | None = None) -> str:
    """Write a deployment and a one-package catalog. Returns the catalog text."""

    root.mkdir(parents=True, exist_ok=True)
    write_empty_lock(root)
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    lock["layout"] = LAYOUT
    if resources:
        lock["resources"] = resources
    (root / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "docker-compose.packages.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "packages").mkdir()
    (root / "keystonectl").mkdir()
    (root / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries: []\n", encoding="utf-8"
    )
    (root / "keystonectl" / "catalog-snapshot.yaml").write_text(
        "active: []\nreserved: []\nwip: []\n", encoding="utf-8"
    )
    packages = []
    for name in manifests:
        packages.append(
            f"  {name}:\n"
            "    kind: service\n"
            "    summary: test\n"
            f"    source: https://example.com/{name}.git\n"
            "    versions:\n"
            "      - version: 1.0.0\n"
            "        ref: v1.0.0\n"
            '        keystone_mcp: ">=0.5.0,<2.0"\n'
        )
    catalog = "schema_version: 1\nname: local\ndescription: test\npackages:\n" + "".join(packages)
    (root / "sources.yaml").write_text(
        "schema_version: 1\nsources:\n"
        "  - name: local\n"
        "    url: https://example.com/catalog.git\n"
        "    ref: v1.0.0\n"
        "    priority: 100\n",
        encoding="utf-8",
    )
    cache = root / ".keystonectl" / "catalogs" / "local"
    cache.mkdir(parents=True)
    (cache / "catalog.yaml").write_text(catalog, encoding="utf-8")
    return catalog


def runner_for(manifests: dict[str, str], box: DockerBox, catalog: str) -> FakeCommandRunner:
    def ls_remote(args, **_kwargs):
        return CommandResult(tuple(args), 0, f"{SHA}\trefs/tags/v1.0.0\n", "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        url = str(args[-2])
        if "catalog" in url:
            (dest / "catalog.yaml").write_text(catalog, encoding="utf-8")
        else:
            for name, text in manifests.items():
                if name in url:
                    (dest / "keystone-service.yaml").write_text(text, encoding="utf-8")
                    break
        (dest / ".git").mkdir(exist_ok=True)
        return CommandResult(tuple(args), 0, "", "")

    ok = CommandResult((), 0, "", "")
    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "init"], lambda args, **_k: ok),
            (["git", "fetch"], ok),
            (["git", "checkout"], ok),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
            (["docker"], box),
        ]
    )


def apply_package(
    root: Path,
    name: str,
    manifests: dict[str, str],
    *,
    box: DockerBox | None = None,
    sets: dict[str, str] | None = None,
    resources: list | None = None,
    environ: dict[str, str] | None = None,
    bind_files: dict[str, str] | None = None,
    reset: bool = True,
):
    if reset:
        catalog = prepare_root(root, manifests, resources=resources)
        for relative, text in (bind_files or {}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    else:
        catalog = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()
    box = box or DockerBox()
    runner = runner_for(manifests, box, catalog)
    result = run_service(
        root,
        "apply",
        name,
        runner=runner,
        secrets=SecretRegistry(),
        sets=sets or {},
        environ=environ or {"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
        deployer=__import__(
            "keystonectl.generate.install", fromlist=["ComposeDeployer"]
        ).ComposeDeployer(),
        defer_public=True,
    )
    return result, runner, box


S3_RESOURCE = {
    "kind": "s3",
    "interface_version": 1,
    "origin": "external",
    "provider": None,
    "params": {"endpoint": "http://s3.internal", "region": "us-east-1", "path_style": True},
    "consumers": [
        {"service": "file-mcp", "bucket": "file-mcp", "provisioned_at": "2026-10-13T00:00:00Z"}
    ],
}

FILE_BIND = (
    "FILE_MCP_S3_ENDPOINT=http://s3.internal\n"
    "FILE_MCP_S3_REGION=us-east-1\n"
    "FILE_MCP_S3_BUCKET=file-mcp\n"
    "FILE_MCP_S3_ACCESS_KEY=key\n"
    "FILE_MCP_S3_SECRET_KEY=secret\n"
    "FILE_MCP_S3_PATH_STYLE=true\n"
)
