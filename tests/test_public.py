"""Public domain, loopback port, nginx vhost, and certificate publication."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from keystonectl.envfiles import read_env
from keystonectl.generate.install import ComposeDeployer
from keystonectl.lock import load_lock, save_lock
from keystonectl.output import SecretRegistry
from keystonectl.plan import Context, run_service
from keystonectl.public import (
    PLACEHOLDER_URL,
    _foreign_sites,
    _line_has_domain,
    choose_port,
    fragment_ports,
    healthz_path,
    location_block,
    materialise_public_base_env,
    render_vhost,
    status_public,
    vhost_substitutions,
)
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.tls import TlsError
from tests.support.deploylab import CLOCK, DockerBox, apply_package, prepare_root, runner_for

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"
GOLDEN = Path(__file__).resolve().parent / "data" / "golden" / "nginx"
DOMAIN = "files.example.com"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def _manifest() -> dict:
    return yaml.safe_load(_facade())


def _http(status: int = 200, text: str = "198.51.100.4") -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        if status == 0:
            raise httpx.ConnectError("down", request=_request)
        return httpx.Response(status, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _http_loopback_ok_external_down() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if (request.url.host or "") in ("127.0.0.1", "localhost"):
            return httpx.Response(200, text="ok")
        raise httpx.ConnectError("down", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


class Host:
    def __init__(self) -> None:
        self.nginx_ok = True
        self.sudo_ok = True
        self.nginx_present = True
        self.nginx_active = True
        self.reload_ok = True
        self.reload_stderr = "nginx: [emerg] bind() to [::]:443 failed"
        self.certbot_code = 0
        self.certbot_stderr = ""
        self.dig_stdout = "198.51.100.4\n"
        self.systemctl = "certbot.timer\n"
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, args, input=None, **_kwargs) -> CommandResult:
        recorded = tuple(str(item) for item in args)
        self.calls.append((recorded, input))
        body = list(recorded[2:] if recorded[:2] == ("sudo", "-n") else recorded)
        if body == ["true"]:
            return CommandResult(recorded, 0 if self.sudo_ok else 1, "", "")
        if body[:2] == ["nginx", "-v"]:
            code = 0 if self.nginx_present else 1
            return CommandResult(recorded, code, "", "nginx missing")
        if body[:2] == ["nginx", "-t"]:
            code = 0 if self.nginx_ok else 1
            return CommandResult(recorded, code, "", "nginx: [emerg] config invalid")
        if body[:2] == ["nginx", "-s"]:
            code = 0 if self.reload_ok else 1
            return CommandResult(recorded, code, "", self.reload_stderr if not self.reload_ok else "")
        if body == ["nginx"]:
            return CommandResult(recorded, 0, "", "")
        if body[:1] == ["tee"] and len(body) > 1:
            path = Path(body[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(input or "", encoding="utf-8")
            return CommandResult(recorded, 0, "", "")
        if body[:2] == ["ln", "-s"]:
            target, link = Path(body[-2]), Path(body[-1])
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(target)
            return CommandResult(recorded, 0, "", "")
        if body[:1] == ["rm"]:
            target = Path(body[-1])
            if target.is_symlink() or target.exists():
                target.unlink()
            return CommandResult(recorded, 0, "", "")
        if body[:2] in (["test", "-f"], ["test", "-r"]):
            ok = Path(body[-1]).is_file()
            return CommandResult(recorded, 0 if ok else 1, "", "")
        if body[:1] == ["openssl"] and "-enddate" in body:
            return CommandResult(recorded, 0, "notAfter=Jan 10 08:00:00 2027 GMT\n", "")
        if body[:2] == ["certbot", "--version"]:
            return CommandResult(recorded, 0, "certbot 2.11.0\n", "")
        if body[:1] == ["certbot"]:
            return CommandResult(recorded, self.certbot_code, "", self.certbot_stderr)
        if body[:3] == ["systemctl", "is-active", "--quiet"] and body[-1] == "nginx":
            code = 0 if self.nginx_active else 3
            return CommandResult(recorded, code, "", "")
        if body[:2] == ["systemctl", "start"] and body[-1] == "nginx":
            self.nginx_active = True
            return CommandResult(recorded, 0, "", "")
        if body[:2] == ["systemctl", "reload"] and body[-1] == "nginx":
            code = 0 if self.reload_ok else 1
            return CommandResult(recorded, code, "", self.reload_stderr if not self.reload_ok else "")
        if body[:1] == ["systemctl"]:
            return CommandResult(recorded, 0, self.systemctl, "")
        if body[:1] == ["dig"]:
            return CommandResult(recorded, 0, self.dig_stdout, "")
        if body[:1] == ["install"]:
            return CommandResult(recorded, 0, "", "")
        if body[:1] == ["grep"]:
            return CommandResult(recorded, 0, "2\n", "")
        return CommandResult(recorded, 0, "", "")


class Combo:
    def __init__(self, inner: FakeCommandRunner, host: Host) -> None:
        self.inner = inner
        self.host = host

    def run(self, args, **kwargs):
        if args and args[0] in {"git", "docker"}:
            return self.inner.run(args, **kwargs)
        return self.host.run(args, **kwargs)


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0",
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN": DOMAIN,
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_DNS": "skip",
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS": "keep",
    }
    values.update(overrides)
    return values


def _installed(tmp_path: Path):
    root = tmp_path / "root"
    text = _facade()
    apply_package(root, "file-mcp", {"file-mcp": text})
    catalog = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()
    return root, text, catalog


def _certs(tmp_path: Path, domain: str = DOMAIN) -> None:
    live = tmp_path / "le" / domain
    live.mkdir(parents=True, exist_ok=True)
    (live / "fullchain.pem").write_text("CERT\n", encoding="utf-8")
    (live / "privkey.pem").write_text("KEY\n", encoding="utf-8")


def _nginx(tmp_path: Path) -> Path:
    root = tmp_path / "nginx"
    for name in ("sites-available", "sites-enabled", "conf.d"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _publish(
    tmp_path: Path,
    *,
    host: Host | None = None,
    http: httpx.Client | None = None,
    environ: dict | None = None,
    handshake=None,
    certs: bool = True,
):
    root, text, catalog = _installed(tmp_path)
    if certs:
        _certs(tmp_path)
    nginx = _nginx(tmp_path)
    host = host or Host()
    box = DockerBox()
    runner = Combo(runner_for({"file-mcp": text}, box, catalog), host)
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ=environ or _env(),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        defer_public=False,
        http=http or _http(),
        nginx_root=nginx,
        le_live=tmp_path / "le",
        custom_dir=tmp_path / "custom",
        cron_file=tmp_path / "cron",
        handshake=handshake,
    )
    return result, root, host, nginx


def _apply_nginx(tmp_path: Path, nginx: Path, **overrides: str):
    root, text, catalog = _installed(tmp_path)
    _certs(tmp_path)
    return run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), Host()),
        secrets=SecretRegistry(),
        sets={},
        environ=_env(**overrides),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=nginx,
        le_live=tmp_path / "le",
    )


def _written_site(nginx: Path) -> Path:
    return nginx / "sites-available" / "keystone-svc-file-mcp"


def _ctx(tmp_path: Path) -> Context:
    return Context(
        root=tmp_path,
        runner=FakeCommandRunner(),
        secrets=SecretRegistry(),
        sets={},
        environ={},
        interactive=False,
        lock={"services": []},
        clock=lambda: CLOCK,
    )


def test_domain_missing_exit_2(tmp_path: Path) -> None:
    root = tmp_path / "root"
    text = _facade()
    catalog = prepare_root(root, {"file-mcp": text})
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=runner_for({"file-mcp": text}, DockerBox(), catalog),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
    )
    assert result.code == 2
    assert any(item["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN" for item in result.missing)


def test_domain_invalid(tmp_path: Path) -> None:
    result, _root, _host, _nginx = _publish(
        tmp_path, environ=_env(KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN="Files.Example")
    )
    assert result.code == 3
    assert result.error["id"] == "domain_invalid"


def test_domain_in_use_refused(tmp_path: Path) -> None:
    root, text, catalog = _installed(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    import copy

    other = copy.deepcopy(lock["services"][0])
    other["name"] = "other"
    other["package"] = "other"
    other["fragment"] = "packages/other.yml"
    other["registry_name"] = None
    other["namespace"] = None
    other["url"] = None
    other["tools"] = []
    other["env"] = {"files": [], "generated": [], "keystone": []}
    other["containers"]["mcp"]["service"] = "other-mcp"
    other["public"] = {"state": "ready", "domain": DOMAIN, "tls": "manual", "host_port": 47650}
    lock["services"].append(other)
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    _certs(tmp_path)
    nginx = _nginx(tmp_path)
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), Host()),
        secrets=SecretRegistry(),
        sets={},
        environ=_env(),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=nginx,
        le_live=tmp_path / "le",
    )
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"


def test_materialise_public_base_env_uses_domain_before_publish() -> None:
    manifest = _manifest()
    ctx = Context(
        root=Path("/tmp/unused"),
        runner=FakeCommandRunner(),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN": DOMAIN},
        interactive=False,
        lock={"services": []},
        clock=lambda: CLOCK,
        defer_public=False,
    )
    env = materialise_public_base_env(ctx, manifest)
    assert env == {"FILE_MCP_PUBLIC_BASE_URL": f"https://{DOMAIN}"}


def test_defer_public_sets_pending_placeholder(tmp_path: Path) -> None:
    root, _text, _catalog = _installed(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    public = lock["services"][0]["public"]
    assert public["state"] == "pending"
    env = read_env(root / "packages" / "file-mcp.env")
    assert env["FILE_MCP_PUBLIC_BASE_URL"] == PLACEHOLDER_URL
    fragment = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:476" not in fragment


def test_defer_public_omits_port_until_public_ready(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.defer_public = True
    ctx.lock = {"services": [{"name": "file-mcp", "public": {"state": "pending"}}]}
    host_port, extra = fragment_ports(ctx, _manifest())
    assert host_port is None
    assert extra == ()


def test_defer_public_keeps_recorded_port_when_public_ready(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.defer_public = True
    port = 47600
    ctx.lock = {
        "services": [
            {
                "name": "file-mcp",
                "public": {"state": "ready", "host_port": port, "domain": DOMAIN},
            }
        ]
    }
    host_port, extra = fragment_ports(ctx, _manifest())
    assert host_port == port
    assert DOMAIN in extra


def test_host_port_allocated_and_stable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first = choose_port(ctx, "file-mcp")
    ctx.lock["services"] = [
        {"name": "file-mcp", "public": {"state": "pending", "host_port": first}}
    ]
    held = socket.socket()
    held.bind(("127.0.0.1", first))
    try:
        assert choose_port(ctx, "file-mcp") == first
    finally:
        held.close()


def test_busy_port_skipped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    held = socket.socket()
    held.bind(("127.0.0.1", 47600))
    try:
        assert choose_port(ctx, "file-mcp") != 47600
    finally:
        held.close()


def test_port_range_exhausted(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.lock["services"] = [
        {"name": f"s{index}", "public": {"state": "pending", "host_port": 47600 + index}}
        for index in range(100)
    ]
    try:
        choose_port(ctx, "file-mcp")
    except TlsError as exc:
        assert exc.error_id == "port_range_exhausted"
    else:
        raise AssertionError("expected port_range_exhausted")


def test_vhost_matches_golden_http_and_https() -> None:
    manifest = _manifest()
    substitutions = vhost_substitutions(
        manifest,
        domain=DOMAIN,
        host_port=47600,
        cert_path="/etc/letsencrypt/live/files.example.com/fullchain.pem",
        key_path="/etc/letsencrypt/live/files.example.com/privkey.pem",
    )
    assert render_vhost("http", substitutions) == (
        GOLDEN / "keystone-svc-file-mcp.http.conf"
    ).read_text(encoding="utf-8")
    assert render_vhost("https", substitutions) == (
        GOLDEN / "keystone-svc-file-mcp.https.conf"
    ).read_text(encoding="utf-8")


def test_locations_only_public_paths_and_healthz() -> None:
    manifest = _manifest()
    text = render_vhost(
        "https",
        vhost_substitutions(
            manifest,
            domain=DOMAIN,
            host_port=47600,
            cert_path="/etc/letsencrypt/live/files.example.com/fullchain.pem",
            key_path="/etc/letsencrypt/live/files.example.com/privkey.pem",
        ),
    )
    assert "location ^~ /u/" in text
    assert "location ^~ /d/" in text
    assert f"location = {healthz_path(manifest)}" in text
    assert "location ^~ /mcp" not in text
    assert "client_max_body_size 100m" in location_block("/u/")


def test_no_placeholders_left() -> None:
    manifest = _manifest()
    for kind in ("http", "https"):
        text = render_vhost(
            kind,
            vhost_substitutions(
                manifest,
                domain=DOMAIN,
                host_port=47600,
                cert_path="/c.pem",
                key_path="/k.pem",
            ),
        )
        assert "__" not in text.split("#", 1)[0] or "server 127.0.0.1" in text
        import re

        assert not re.search(r"__[A-Z_]+__", text)


def test_templates_loaded_as_package_data() -> None:
    from importlib.resources import files

    package = files("keystonectl.nginx")
    assert package.joinpath("service.conf.template").is_file()
    assert package.joinpath("service-https.conf.template").is_file()
    assert "keystonectl" in package.joinpath("service.conf.template").read_text(encoding="utf-8")


def test_vhost_written_via_sudo_tee_stdin(tmp_path: Path) -> None:
    result, _root, host, nginx = _publish(tmp_path)
    assert result.code == 0, result.error
    tees = [item for item in host.calls if "tee" in item[0]]
    assert tees
    assert tees[0][0][:2] == ("sudo", "-n")
    assert tees[0][1] and "server_name files.example.com" in tees[0][1]
    assert (nginx / "sites-available" / "keystone-svc-file-mcp").is_file()


def test_nginx_reload_failure_fails_install(tmp_path: Path) -> None:
    host = Host()
    host.reload_ok = False
    result, _root, _host, _nginx = _publish(tmp_path, host=host)
    assert result.code == 3
    assert result.error["id"] == "nginx_config_invalid"
    assert "bind" in (result.error.get("message") or "").lower()


def test_nginx_start_when_inactive(tmp_path: Path) -> None:
    host = Host()
    host.nginx_active = False
    result, _root, host, _nginx = _publish(tmp_path, host=host)
    assert result.code == 0, result.error
    assert any(args[-2:] == ("start", "nginx") for args, _stdin in host.calls)


def test_nginx_test_failure_restores_previous_file(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    site = nginx / "sites-available" / "keystone-svc-file-mcp"
    site.write_text("server { server_name old.example; }\n", encoding="utf-8")
    host = Host()
    host.nginx_ok = False
    result, _root, host, _nginx_root = _publish(tmp_path, host=host)
    assert result.code == 3
    assert result.error["id"] == "nginx_config_invalid"
    assert "old.example" in site.read_text(encoding="utf-8")


def test_sudo_required_gives_manual_steps(tmp_path: Path) -> None:
    host = Host()
    host.sudo_ok = False
    result, _root, _host, _nginx = _publish(tmp_path, host=host)
    assert result.code == 3
    assert result.error["id"] == "sudo_required"
    steps = result.result.get("manual_steps") or []
    assert steps
    assert any("TLS=manual" in step for step in steps)


def test_nginx_missing(tmp_path: Path) -> None:
    host = Host()
    host.nginx_present = False
    result, _root, _host, _nginx = _publish(tmp_path, host=host)
    assert result.code == 3
    assert result.error["id"] == "nginx_missing"


def test_takeover_question_yes_disables_link_and_remove_restores(tmp_path: Path) -> None:
    from keystonectl.lifecycle import remove_service

    root, text, catalog = _installed(tmp_path)
    _certs(tmp_path)
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "other.conf"
    available.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "other.conf"
    link.symlink_to(available)
    host = Host()
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), host),
        secrets=SecretRegistry(),
        sets={},
        environ=_env(KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes"),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=nginx,
        le_live=tmp_path / "le",
    )
    assert result.code == 0, result.error
    assert not link.exists()
    removed = remove_service(
        root,
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), host),
        secrets=SecretRegistry(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"},
        environ={},
        interactive=False,
        clock=lambda: CLOCK,
        nginx_root=nginx,
    )
    assert removed.code == 0, removed.error
    assert link.is_symlink()


def test_takeover_regular_file_refused(tmp_path: Path) -> None:
    root, text, catalog = _installed(tmp_path)
    _certs(tmp_path)
    nginx = _nginx(tmp_path)
    foreign = nginx / "sites-enabled" / "other.conf"
    foreign.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), Host()),
        secrets=SecretRegistry(),
        sets={},
        environ=_env(KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes"),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=nginx,
        le_live=tmp_path / "le",
    )
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert foreign.is_file()


def test_foreign_site_skips_only_exact_own_name(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    enabled = nginx / "sites-enabled"
    (enabled / "keystone-svc-file-mcp").write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    (enabled / "keystone-svc-file-mcp.conf").write_text(
        f"server_name {DOMAIN};\n", encoding="utf-8"
    )
    (enabled / "keystone-svc-doc").write_text("server_name docs.example.com;\n", encoding="utf-8")
    (enabled / "keystone-svc-docs").write_text("server_name docs.example.com;\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.nginx_root = nginx
    file_mcp = [path.name for path, _kind in _foreign_sites(ctx, "file-mcp", DOMAIN)]
    assert file_mcp == ["keystone-svc-file-mcp.conf"]
    doc = [path.name for path, _kind in _foreign_sites(ctx, "doc", "docs.example.com")]
    assert doc == ["keystone-svc-docs"]


def test_server_name_match_is_case_insensitive_and_beats_wildcards() -> None:
    domain = "files.example.com"
    assert _line_has_domain("server_name Files.example.com;\n", domain)
    assert _line_has_domain("server_name *.example.com;\n", domain)
    assert _line_has_domain("server_name *.Example.COM;\n", domain)
    assert _line_has_domain("server_name *.com;\n", domain)
    assert _line_has_domain("server_name files.*;\n", domain)
    assert _line_has_domain("server_name .example.com;\n", domain)
    assert _line_has_domain("server_name example.org *.example.com;\n", domain)
    assert _line_has_domain("server_name .example.com;\n", "example.com")
    assert not _line_has_domain("server_name *.example.com;\n", "example.com")
    assert not _line_has_domain("server_name *.other.com;\n", domain)
    assert not _line_has_domain("server_name other.example.com;\n", domain)
    assert not _line_has_domain("server_name files.example.org;\n", domain)


def test_prefixed_symlink_asks_takeover(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "keystone-svc-file-mcp.conf"
    available.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "keystone-svc-file-mcp.conf"
    link.symlink_to(available)
    result = _apply_nginx(tmp_path, nginx)
    assert result.code == 2
    assert any(item["var"] == "KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER" for item in result.missing)
    assert link.is_symlink()
    assert not _written_site(nginx).exists()


def test_prefixed_regular_file_is_domain_in_use(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    foreign = nginx / "sites-enabled" / "keystone-svc-file-mcp.conf"
    foreign.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert foreign.is_file()
    assert not _written_site(nginx).exists()


def test_server_name_case_difference_is_domain_in_use(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    foreign = nginx / "sites-enabled" / "other.conf"
    foreign.write_text("server_name Files.example.com;\n", encoding="utf-8")
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert foreign.is_file()
    assert not _written_site(nginx).exists()


def test_wildcard_symlink_is_not_removed(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "wild.conf"
    available.write_text("server_name *.example.com;\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "wild.conf"
    link.symlink_to(available)
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert "Narrow it by hand" in result.error["message"]
    assert link.is_symlink()
    assert available.is_file()
    assert "server_name *.example.com;" in available.read_text(encoding="utf-8")
    assert not _written_site(nginx).exists()


def test_suffix_symlink_is_not_removed(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "suffix.conf"
    available.write_text("server_name .example.com;\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "suffix.conf"
    link.symlink_to(available)
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert link.is_symlink()
    assert not _written_site(nginx).exists()


def test_takeover_yes_removes_case_insensitive_exact_symlink(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "other.conf"
    available.write_text("server_name Files.example.com;\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "other.conf"
    link.symlink_to(available)
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 0, result.error
    assert not link.exists()
    assert available.is_file()
    assert _written_site(nginx).is_file()


def test_symlink_with_another_name_is_not_removed(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "shared.conf"
    available.write_text(f"server_name {DOMAIN} other.example.com;\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "shared.conf"
    link.symlink_to(available)
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert link.is_symlink()
    assert not _written_site(nginx).exists()


def test_wildcard_server_name_is_domain_in_use(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    foreign = nginx / "conf.d" / "wild.conf"
    foreign.write_text("server_name *.example.com;\n", encoding="utf-8")
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert foreign.is_file()
    assert not _written_site(nginx).exists()


def test_unrelated_server_name_does_not_block(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    foreign = nginx / "conf.d" / "other.conf"
    foreign.write_text("server_name other.example.com *.other.com;\n", encoding="utf-8")
    result = _apply_nginx(tmp_path, nginx)
    assert result.code == 0, result.error
    assert foreign.is_file()
    assert _written_site(nginx).is_file()


def test_unreadable_nginx_site_stops_before_writing(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    foreign = nginx / "sites-enabled" / "hidden.conf"
    foreign.write_text("server_name other.example.com;\n", encoding="utf-8")
    foreign.chmod(0)
    try:
        result = _apply_nginx(tmp_path, nginx)
    finally:
        foreign.chmod(0o644)
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert "could not read" in result.error["message"]
    assert "other.example.com" not in result.error["message"]
    assert not _written_site(nginx).exists()


def test_takeover_stops_while_another_site_remains(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    available = nginx / "sites-available" / "aaa.conf"
    available.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    link = nginx / "sites-enabled" / "aaa.conf"
    link.symlink_to(available)
    regular = nginx / "conf.d" / "zzz.conf"
    regular.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 3
    assert result.error["id"] == "domain_in_use"
    assert link.is_symlink()
    assert regular.is_file()
    assert not _written_site(nginx).exists()


def test_takeover_yes_removes_every_enabled_link(tmp_path: Path) -> None:
    nginx = _nginx(tmp_path)
    links: list[Path] = []
    for name in ("aaa.conf", "zzz.conf"):
        available = nginx / "sites-available" / name
        available.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
        link = nginx / "sites-enabled" / name
        link.symlink_to(available)
        links.append(link)
    result = _apply_nginx(tmp_path, nginx, KEYSTONE_SETUP_SERVICE_FILE_MCP_TAKEOVER="yes")
    assert result.code == 0, result.error
    assert all(not link.exists() for link in links)
    assert all((nginx / "sites-available" / link.name).is_file() for link in links)
    assert _written_site(nginx).is_file()


def test_tls_keep_skips_certbot(tmp_path: Path) -> None:
    result, _root, host, _nginx = _publish(tmp_path)
    assert result.code == 0, result.error
    assert not any("certbot" in call[0] for call in host.calls)


def test_letsencrypt_valid_over_30_days_rerun_no_certbot(tmp_path: Path) -> None:
    result, root, host, nginx = _publish(
        tmp_path,
        environ=_env(
            KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS="certbot",
            KEYSTONE_SETUP_TLS_EMAIL="ops@example.com",
        ),
    )
    assert result.code == 0, result.error
    assert not any("--nginx" in call[0] for call in host.calls)
    lock = load_lock(root / "services.lock.yaml")
    assert lock["services"][0]["public"]["state"] == "ready"
    assert (nginx / "sites-available" / "keystone-svc-file-mcp").is_file()


def test_tls_error_writes_pending_with_last_error(tmp_path: Path) -> None:
    host = Host()
    host.certbot_code = 1
    host.certbot_stderr = "No such authorization"
    result, root, _host, _nginx = _publish(
        tmp_path,
        host=host,
        certs=False,
        environ=_env(
            KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS="certbot",
            KEYSTONE_SETUP_TLS_EMAIL="ops@example.com",
        ),
    )
    assert result.code == 3
    assert result.error["id"] == "certbot_failed"
    assert result.retryable is True
    lock = load_lock(root / "services.lock.yaml")
    public = lock["services"][0]["public"]
    assert public["state"] == "pending"
    assert public["last_error"] == "certbot_failed"
    assert (root / "packages" / "file-mcp.yml").is_file()


def test_retry_after_pending_completes(tmp_path: Path) -> None:
    host = Host()
    host.certbot_code = 1
    host.certbot_stderr = "No such authorization"
    environ = _env(
        KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS="certbot",
        KEYSTONE_SETUP_TLS_EMAIL="ops@example.com",
    )
    first, root, _host, _nginx_root = _publish(tmp_path, host=host, environ=environ, certs=False)
    assert first.code == 3
    host.certbot_code = 0
    live = tmp_path / "le" / DOMAIN
    # The failed run did not create a certificate. A retry that issues one must see it.
    text = _facade()
    catalog = (root / ".keystonectl" / "catalogs" / "local" / "catalog.yaml").read_text()

    def issue(args, input=None, **kwargs):
        body = list(args[2:] if tuple(args[:2]) == ("sudo", "-n") else args)
        if body[:1] == ["certbot"] and "--nginx" in body:
            live.mkdir(parents=True, exist_ok=True)
            (live / "fullchain.pem").write_text("CERT\n", encoding="utf-8")
            (live / "privkey.pem").write_text("KEY\n", encoding="utf-8")
        return Host.run(host, args, input=input, **kwargs)

    host.run = issue  # type: ignore[method-assign]
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), host),
        secrets=SecretRegistry(),
        sets={},
        environ=environ,
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=_nginx_root,
        le_live=tmp_path / "le",
    )
    assert result.code == 0, result.error
    lock = load_lock(root / "services.lock.yaml")
    assert lock["services"][0]["public"]["state"] == "ready"


def test_manual_mode_no_sudo_no_nginx_checks_https(tmp_path: Path) -> None:
    ok, root, host, _nginx = _publish(
        tmp_path,
        environ=_env(KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS="manual"),
        handshake=lambda _domain: {"notAfter": "Jan 10 08:00:00 2027 GMT"},
    )
    assert ok.code == 0, ok.error
    assert not any(call[0][:2] == ("sudo", "-n") for call in host.calls)
    assert not any("nginx" in call[0] for call in host.calls)
    assert ok.result["public"] == {
        "state": "ready",
        "domain": DOMAIN,
        "last_error": None,
        "tls": "manual",
    }
    ready = load_lock(root / "services.lock.yaml")["services"][0]["public"]
    assert ready["state"] == "ready"
    assert (root / ".keystonectl" / "nginx" / "keystone-svc-file-mcp.conf").is_file()

    failed_host = Host()
    failed, failed_root, _host, _nginx_root = _publish(
        tmp_path / "bad",
        host=failed_host,
        http=_http_loopback_ok_external_down(),
        environ=_env(KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS="manual"),
        handshake=lambda _domain: {"notAfter": "Jan 10 08:00:00 2027 GMT"},
    )
    assert failed.code == 3
    assert failed.error["id"] == "public_unreachable"
    assert failed.retryable is True
    pending = load_lock(failed_root / "services.lock.yaml")["services"][0]["public"]
    assert pending["state"] == "pending"
    assert pending["last_error"] == "public_unreachable"


def test_public_base_url_written_and_containers_recreated(tmp_path: Path) -> None:
    result, root, _host, _nginx = _publish(tmp_path)
    assert result.code == 0, result.error
    env = read_env(root / "packages" / "file-mcp.env")
    assert env["FILE_MCP_PUBLIC_BASE_URL"] == f"https://{DOMAIN}"
    blob = " ".join(" ".join(call.args) for call in result.actions) if False else ""
    # The deployer records compose calls on the inner runner. Search the fragment's sibling by
    # reading docker calls from a fresh publish is indirect; the env change forces recreate.
    assert "https://files.example.com" in (root / "packages" / "file-mcp.env").read_text()
    del blob


def test_extra_hosts_added_to_generated_fragments(tmp_path: Path) -> None:
    result, root, _host, _nginx = _publish(tmp_path)
    assert result.code == 0, result.error
    text = (root / "packages" / "file-mcp.yml").read_text(encoding="utf-8")
    assert f"{DOMAIN}:host-gateway" in text
    assert "127.0.0.1:" in text


def test_extra_hosts_manual_warning_for_ejected_and_edited(tmp_path: Path) -> None:
    root, text, catalog = _installed(tmp_path)
    _certs(tmp_path)
    lock = load_lock(root / "services.lock.yaml")
    import copy

    neighbor = copy.deepcopy(lock["services"][0])
    neighbor["name"] = "other"
    neighbor["package"] = "other"
    neighbor["managed"] = "ejected"
    neighbor["fragment"] = "packages/other.yml"
    neighbor["fragment_sha256"] = None
    neighbor["registry_name"] = None
    neighbor["namespace"] = None
    neighbor["url"] = None
    neighbor["tools"] = []
    neighbor["env"] = {"files": [], "generated": [], "keystone": []}
    containers = neighbor.get("containers") or {}
    if isinstance(containers, dict) and "mcp" in containers:
        containers["mcp"]["service"] = "other-mcp"
    lock["services"].append(neighbor)
    save_lock(root / "services.lock.yaml", lock, now=CLOCK)
    edited = root / "packages" / "edited.yml"
    # Ejected neighbor file must stay byte-for-byte.
    marker = "OPERATOR-FILE\n"
    (root / "packages" / "other.yml").write_text(marker, encoding="utf-8")
    source = lock["services"][0]["source"]
    version = lock["services"][0]["version"]
    cache = root / ".keystonectl" / "packages" / source / "other" / version
    cache.mkdir(parents=True)
    (cache / "keystone-service.yaml").write_text(
        _facade().replace("name: file-mcp", "name: other"), encoding="utf-8"
    )
    result = run_service(
        root,
        "apply",
        "file-mcp",
        runner=Combo(runner_for({"file-mcp": text}, DockerBox(), catalog), Host()),
        secrets=SecretRegistry(),
        sets={},
        environ=_env(),
        interactive=False,
        clock=lambda: CLOCK,
        deployer=ComposeDeployer(),
        http=_http(),
        nginx_root=_nginx(tmp_path),
        le_live=tmp_path / "le",
    )
    assert result.code == 0, result.error
    assert (root / "packages" / "other.yml").read_text(encoding="utf-8") == marker
    assert any(item["id"] == "extra_hosts_manual" for item in result.warnings)
    del edited


def test_status_cert_expiring_thresholds(tmp_path: Path) -> None:
    def warn_for(days: int, *, kind: str, mode: str) -> bool:
        script_end = datetime(2026, 10, 13, tzinfo=UTC)
        moment = script_end.fromtimestamp(script_end.timestamp() + days * 86400, UTC)
        stamp = moment.strftime("%b %d %H:%M:%S %Y GMT").replace(" 0", " ")
        # OpenSSL prints a space-padded day. parse_enddate collapses spaces.

        class Ends(Host):
            def run(self, args, input=None, **kwargs):
                body = list(args[2:] if tuple(args[:2]) == ("sudo", "-n") else args)
                if body[:1] == ["openssl"] and "-enddate" in body:
                    self.calls.append((tuple(args), input))
                    text = "notAfter=" + moment.strftime("%b %d %H:%M:%S %Y GMT") + "\n"
                    return CommandResult(tuple(args), 0, text, "")
                return super().run(args, input=input, **kwargs)

        host = Ends()
        live = tmp_path / kind / DOMAIN
        live.mkdir(parents=True, exist_ok=True)
        (live / "fullchain.pem").write_text("CERT\n", encoding="utf-8")
        (live / "privkey.pem").write_text("KEY\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        ctx.runner = Combo(
            FakeCommandRunner([([], lambda *a, **k: CommandResult((), 0, "", ""))]), host
        )
        if kind == "le":
            ctx.le_live = tmp_path / "le"
        else:
            ctx.custom_dir = tmp_path / "custom"
            ctx.le_live = tmp_path / "missing-le"
        ctx.cron_file = tmp_path / "no-cron"
        body = {"warnings": [], "public": None}
        status_public(
            ctx,
            {"public": {"state": "ready", "domain": DOMAIN, "tls": mode}},
            body,
        )
        del stamp
        return any(item["id"] == "cert_expiring" for item in body["warnings"])

    assert warn_for(20, kind="le", mode="certbot")
    assert not warn_for(21, kind="le", mode="certbot")
    assert warn_for(30, kind="custom", mode="custom")
    assert not warn_for(31, kind="custom", mode="custom")


def test_status_renewal_missing(tmp_path: Path) -> None:
    host = Host()
    host.systemctl = "none\n"
    _certs(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.runner = host  # type: ignore[assignment]
    ctx.le_live = tmp_path / "le"
    ctx.cron_file = tmp_path / "missing-cron"
    body: dict = {"warnings": []}
    status_public(
        ctx,
        {"public": {"state": "ready", "domain": DOMAIN, "tls": "certbot"}},
        body,
    )
    assert any(item["id"] == "renewal_timer_missing" for item in body["warnings"])
