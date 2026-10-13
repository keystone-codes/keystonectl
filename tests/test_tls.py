"""Certificate checks through openssl, dig, and certbot. No real host changes."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.tls import Tls, TlsError, name_covers

CLOCK = datetime(2026, 10, 13, tzinfo=UTC)
FUTURE = "notAfter=Jan 10 08:00:00 2027 GMT\n"
PAST = "notAfter=Jan 1 00:00:00 2020 GMT\n"
DOMAIN = "files.example.com"
MARKER = "SUPER-SECRET-KEY-MATERIAL"


def _client(text: str = "198.51.100.4", *, fail: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


class Script:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.enddate = FUTURE
        self.enddate_bad = False
        self.x509_ok = True
        self.pkey_ok = True
        self.pubs_match = True
        self.checkend0 = True
        self.checkend30 = True
        self.chain_count = "2"
        self.issuer = "CN = Example CA"
        self.subject = "CN = files.example.com"
        self.san = "DNS:files.example.com"
        self.certbot_version = True
        self.certbot_code = 0
        self.certbot_stderr = ""
        self.systemctl = "snap.certbot.renew.timer\n"
        self.dig_code = 0
        self.dig_stdout = "198.51.100.4\n"
        self.unreadable: set[str] = set()
        self.runner = FakeCommandRunner([([], self._dispatch)])

    def _dispatch(self, args, **kwargs) -> CommandResult:
        body = list(args)
        if body[:2] == ["sudo", "-n"]:
            body = body[2:]
        if body[:2] == ["test", "-f"] or body[:2] == ["test", "-r"]:
            path = body[-1]
            if path in self.unreadable or not Path(path).is_file():
                return CommandResult(tuple(args), 1, "", "")
            return CommandResult(tuple(args), 0, "", "")
        if body[:1] == ["dig"]:
            return CommandResult(tuple(args), self.dig_code, self.dig_stdout, "")
        if body[:2] == ["certbot", "--version"]:
            code = 0 if self.certbot_version else 1
            return CommandResult(tuple(args), code, "certbot 2.11.0\n", "")
        if body[:1] == ["certbot"]:
            return CommandResult(tuple(args), self.certbot_code, "", self.certbot_stderr)
        if body[:1] == ["systemctl"]:
            return CommandResult(tuple(args), 0, self.systemctl, "")
        if body[:2] == ["grep", "-c"]:
            return CommandResult(tuple(args), 0, self.chain_count + "\n", "")
        if body[:1] == ["install"]:
            return CommandResult(tuple(args), 0, "", "")
        if body[:2] == ["openssl", "pkey"]:
            text = "KEY\n" if self.pubs_match else "OTHER\n"
            code = 0 if self.pkey_ok else 1
            return CommandResult(tuple(args), code, text, "")
        if body[:2] == ["openssl", "x509"] and "-enddate" in body:
            if self.enddate_bad:
                return CommandResult(tuple(args), 0, "notAfter=nope\n", "")
            return CommandResult(tuple(args), 0, self.enddate, "")
        if body[:2] == ["openssl", "x509"] and "-pubkey" in body:
            return CommandResult(tuple(args), 0, "KEY\n", "")
        if body[:2] == ["openssl", "x509"] and "-checkend" in body:
            seconds = body[body.index("-checkend") + 1]
            ok = self.checkend0 if seconds == "0" else self.checkend30
            return CommandResult(tuple(args), 0 if ok else 1, "", "")
        if body[:2] == ["openssl", "x509"] and "-issuer" in body:
            return CommandResult(
                tuple(args), 0, f"issuer={self.issuer}\nsubject={self.subject}\n", ""
            )
        if body[:2] == ["openssl", "x509"] and "-subject" in body:
            return CommandResult(tuple(args), 0, f"subject= {self.subject}\n", "")
        if body[:2] == ["openssl", "x509"] and "-ext" in body:
            return CommandResult(
                tuple(args), 0, f"X509v3 Subject Alternative Name:\n {self.san}\n", ""
            )
        if body[:2] == ["openssl", "x509"]:
            code = 0 if self.x509_ok else 1
            return CommandResult(tuple(args), code, "", "not a pem")
        return CommandResult(tuple(args), 0, "", "")


def _tls(script: Script, *, root: bool = True, http: httpx.Client | None = None) -> Tls:
    return Tls(
        script.runner,
        clock=lambda: CLOCK,
        le_live=script.root / "le",
        custom_dir=script.root / "custom",
        cron_file=script.root / "cron",
        http=http or _client(),
        is_root=root,
    )


def _pair(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "fullchain.pem").write_text("CERT\n", encoding="utf-8")
    (directory / "privkey.pem").write_text("KEY\n", encoding="utf-8")


def test_detect_none(tmp_path: Path) -> None:
    script = Script(tmp_path)
    info = _tls(script).detect(DOMAIN)
    assert info.kind == "none"
    assert info.not_after is None
    assert info.days_left is None


def test_detect_letsencrypt_paths_and_days_left(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "le" / DOMAIN)
    info = _tls(script).detect(DOMAIN)
    assert info.kind == "letsencrypt"
    assert info.cert_path is not None and info.cert_path.endswith("fullchain.pem")
    assert info.key_path is not None and info.key_path.endswith("privkey.pem")
    assert info.not_after == datetime(2027, 1, 10, 8, 0, tzinfo=UTC)
    assert info.days_left is not None and info.days_left > 30


def test_detect_custom_when_no_letsencrypt(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "custom" / DOMAIN)
    info = _tls(script).detect(DOMAIN)
    assert info.kind == "custom"


def test_detect_letsencrypt_wins(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "le" / DOMAIN)
    _pair(tmp_path / "custom" / DOMAIN)
    assert _tls(script).detect(DOMAIN).kind == "letsencrypt"


def test_detect_expired_negative_days(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.enddate = PAST
    _pair(tmp_path / "le" / DOMAIN)
    info = _tls(script).detect(DOMAIN)
    assert info.days_left is not None and info.days_left < 0


def test_detect_unparseable_enddate(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.enddate_bad = True
    _pair(tmp_path / "le" / DOMAIN)
    tls = _tls(script)
    info = tls.detect(DOMAIN)
    assert info.not_after is None
    assert info.days_left is None
    assert tls.warnings


def test_dns_match(tmp_path: Path) -> None:
    script = Script(tmp_path)
    assert _tls(script).check_dns(DOMAIN) == "198.51.100.4"


def test_dns_no_record_retryable(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.dig_stdout = ""
    with pytest.raises(TlsError) as caught:
        _tls(script).check_dns(DOMAIN)
    assert caught.value.error_id == "dns_no_record"
    assert caught.value.retryable is True
    assert "add an A record" in (caught.value.hint or "")
    assert DOMAIN in (caught.value.hint or "")


def test_dns_mismatch_retryable(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.dig_stdout = "203.0.113.7\n"
    with pytest.raises(TlsError) as caught:
        _tls(script).check_dns(DOMAIN)
    assert caught.value.error_id == "dns_mismatch"
    assert caught.value.retryable is True
    assert "203.0.113.7" in caught.value.message
    assert "198.51.100.4" in caught.value.message


def test_public_ip_unknown(tmp_path: Path) -> None:
    script = Script(tmp_path)
    with pytest.raises(TlsError) as caught:
        _tls(script, http=_client(fail=True)).check_dns(DOMAIN)
    assert caught.value.error_id == "public_ip_unknown"
    assert caught.value.retryable is True


def test_dns_without_dig_uses_system_resolver_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script(tmp_path)
    script.dig_code = 1

    def lookup(host: str, port: int | None, family: int):
        assert host == DOMAIN
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.4", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    tls = _tls(script)
    assert tls.check_dns(DOMAIN) == "198.51.100.4"
    assert any(item["id"] == "dns_system_resolver" for item in tls.warnings)


def _certbot_call(script: Script) -> tuple[str, ...]:
    for call in script.runner.calls:
        if "--nginx" in call.args:
            return call.args
    raise AssertionError("certbot was not invoked")


def test_certbot_exact_arguments(tmp_path: Path) -> None:
    script = Script(tmp_path)
    info = _tls(script).issue_certbot(DOMAIN, "ops@example.com")
    assert info.kind == "none"
    args = _certbot_call(script)
    assert args == (
        "certbot",
        "--nginx",
        "-d",
        DOMAIN,
        "--redirect",
        "--non-interactive",
        "--agree-tos",
        "-m",
        "ops@example.com",
        "--no-eff-email",
    )


def test_certbot_staging_adds_test_cert(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _tls(script).issue_certbot(DOMAIN, "ops@example.com", staging=True)
    assert _certbot_call(script)[-1] == "--test-cert"


def test_certbot_keep_until_expiring_when_valid_over_30_days(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "le" / DOMAIN)
    _tls(script).issue_certbot(DOMAIN, "ops@example.com")
    assert "--keep-until-expiring" in _certbot_call(script)


def test_certbot_failure_retryable_with_stale_order_hint(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.certbot_code = 1
    script.certbot_stderr = (
        "\n".join(f"line {index}" for index in range(25)) + "\nNo such authorization\n"
    )
    with pytest.raises(TlsError) as caught:
        _tls(script).issue_certbot(DOMAIN, "ops@example.com")
    assert caught.value.error_id == "certbot_failed"
    assert caught.value.retryable is True
    assert "stale ACME order" in (caught.value.hint or "")
    assert "line 0" not in caught.value.message
    assert "No such authorization" in caught.value.message


def test_certbot_rate_limit_hint(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.certbot_code = 1
    script.certbot_stderr = "too many failed authorizations"
    with pytest.raises(TlsError) as caught:
        _tls(script).issue_certbot(DOMAIN, "ops@example.com")
    assert "rate limit" in (caught.value.hint or "")
    assert "--certbot-staging" in (caught.value.hint or "")


def test_certbot_missing(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.certbot_version = False
    with pytest.raises(TlsError) as caught:
        _tls(script).issue_certbot(DOMAIN, "ops@example.com")
    assert caught.value.error_id == "certbot_missing"
    assert caught.value.retryable is False
    assert "python3-certbot-nginx" in (caught.value.hint or "")


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    cert.write_text("CERT\n", encoding="utf-8")
    key.write_text("KEY\n", encoding="utf-8")
    return cert, key


def _install_args(script: Script) -> list[tuple[str, ...]]:
    return [call.args for call in script.runner.calls if "install" in call.args[:3]]


def test_custom_valid_pair_installed_with_modes(tmp_path: Path) -> None:
    script = Script(tmp_path)
    cert, key = _sources(tmp_path)
    info = _tls(script).install_custom(DOMAIN, cert, key)
    assert info.kind == "custom"
    commands = _install_args(script)
    assert list(commands[0][:8]) == ["install", "-d", "-m", "700", "-o", "root", "-g", "root"]
    assert list(commands[1][:7]) == ["install", "-m", "644", "-o", "root", "-g", "root"]
    assert list(commands[2][:7]) == ["install", "-m", "600", "-o", "root", "-g", "root"]


def test_custom_key_mismatch(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.pubs_match = False
    cert, key = _sources(tmp_path)
    with pytest.raises(TlsError) as caught:
        _tls(script).install_custom(DOMAIN, cert, key)
    assert caught.value.error_id == "key_mismatch"


def test_custom_der_not_pem(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.x509_ok = False
    cert, key = _sources(tmp_path)
    with pytest.raises(TlsError) as caught:
        _tls(script).install_custom(DOMAIN, cert, key)
    assert caught.value.error_id == "cert_not_pem"
    assert "pkcs12" in (caught.value.hint or "")


def test_custom_key_not_pem(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.pkey_ok = False
    cert, key = _sources(tmp_path)
    with pytest.raises(TlsError) as caught:
        _tls(script).install_custom(DOMAIN, cert, key)
    assert caught.value.error_id == "key_not_pem"


def test_custom_expired(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.checkend0 = False
    cert, key = _sources(tmp_path)
    with pytest.raises(TlsError) as caught:
        _tls(script).install_custom(DOMAIN, cert, key)
    assert caught.value.error_id == "cert_expired"


def test_custom_unreadable(tmp_path: Path) -> None:
    script = Script(tmp_path)
    cert, key = _sources(tmp_path)
    script.unreadable.add(str(cert))
    with pytest.raises(TlsError) as caught:
        _tls(script).install_custom(DOMAIN, cert, key)
    assert caught.value.error_id == "cert_unreadable"


def test_custom_chain_missing_warns(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.chain_count = "1"
    script.issuer = "CN = Example CA"
    script.subject = "CN = files.example.com"
    cert, key = _sources(tmp_path)
    tls = _tls(script)
    tls.install_custom(DOMAIN, cert, key)
    assert any(item["id"] == "chain_missing" for item in tls.warnings)


def test_custom_domain_not_covered_warns(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.subject = "CN = other.example"
    script.san = "DNS:other.example"
    cert, key = _sources(tmp_path)
    tls = _tls(script)
    tls.install_custom(DOMAIN, cert, key)
    assert any(item["id"] == "cert_may_not_cover_domain" for item in tls.warnings)


def test_custom_wildcard_one_label(tmp_path: Path) -> None:
    assert name_covers("*.example.com", "a.example.com")
    assert not name_covers("*.example.com", "a.b.example.com")
    script = Script(tmp_path)
    script.subject = "CN = *.example.com"
    script.san = "DNS:*.example.com"
    cert, key = _sources(tmp_path)
    covered = _tls(script)
    covered.install_custom("a.example.com", cert, key)
    assert not any(item["id"] == "cert_may_not_cover_domain" for item in covered.warnings)
    missed = _tls(script)
    missed.install_custom("a.b.example.com", cert, key)
    assert any(item["id"] == "cert_may_not_cover_domain" for item in missed.warnings)


def test_custom_same_paths_no_copy(tmp_path: Path) -> None:
    script = Script(tmp_path)
    destination = tmp_path / "custom" / DOMAIN
    _pair(destination)
    tls = _tls(script)
    tls.install_custom(DOMAIN, destination / "fullchain.pem", destination / "privkey.pem")
    assert _install_args(script) == []


def test_renewal_systemd_timer(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "le" / DOMAIN)
    assert _tls(script).renewal_check(DOMAIN).mode == "systemd-timer"


def test_renewal_cron(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.systemctl = "none\n"
    (tmp_path / "cron").write_text("0 0 * * * root certbot renew\n", encoding="utf-8")
    _pair(tmp_path / "le" / DOMAIN)
    assert _tls(script).renewal_check(DOMAIN).mode == "cron"


def test_renewal_missing_warns(tmp_path: Path) -> None:
    script = Script(tmp_path)
    script.systemctl = "none\n"
    _pair(tmp_path / "le" / DOMAIN)
    tls = _tls(script)
    assert tls.renewal_check(DOMAIN).mode == "missing"
    assert any(item["id"] == "renewal_timer_missing" for item in tls.warnings)


def test_renewal_custom_operator(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "custom" / DOMAIN)
    tls = _tls(script)
    assert tls.renewal_check(DOMAIN).mode == "operator"
    assert any(item["id"] == "renewal_manual" for item in tls.warnings)


def test_sudo_prefix_only_when_not_root(tmp_path: Path) -> None:
    script = Script(tmp_path)
    _pair(tmp_path / "le" / DOMAIN)
    _tls(script, root=False).detect(DOMAIN)
    assert script.runner.calls[0].args[:2] == ("sudo", "-n")
    rooted = Script(tmp_path)
    _tls(rooted, root=True).detect(DOMAIN)
    assert all(call.args[:2] != ("sudo", "-n") for call in rooted.runner.calls)


def test_key_content_never_in_args_or_output(tmp_path: Path) -> None:
    script = Script(tmp_path)
    cert, key = _sources(tmp_path)
    key.write_text(MARKER + "\n", encoding="utf-8")
    _tls(script).install_custom(DOMAIN, cert, key)
    blob = "\n".join(
        " ".join(call.args) + "\n" + (call.input or "") for call in script.runner.calls
    )
    assert MARKER not in blob
