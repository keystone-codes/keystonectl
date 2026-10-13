"""Certificate lifecycle. Host commands go through CommandRunner. Keys stay on disk."""

from __future__ import annotations

import os
import re
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from keystonectl.errors import ERRORS, KsctlError
from keystonectl.runner import CommandRunner

_IP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me",
    "https://ipinfo.io/ip",
)
_IPV4 = re.compile(
    r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
)
_ENDDATE = re.compile(r"notAfter=(.*)")


class TlsError(KsctlError):
    """A certificate or DNS step failed. ``retryable`` follows the error id."""

    def __init__(self, error_id: str, message: str, *, hint: str | None = None) -> None:
        spec = ERRORS[error_id]
        super().__init__(error_id, message, retryable=spec.retryable, hint=hint or spec.action)


@dataclass(frozen=True)
class CertInfo:
    kind: Literal["letsencrypt", "custom", "none"]
    cert_path: str | None
    key_path: str | None
    not_after: datetime | None
    days_left: int | None


@dataclass(frozen=True)
class Renewal:
    mode: Literal["systemd-timer", "cron", "operator", "missing"]


def parse_enddate(text: str) -> datetime | None:
    """Parse ``notAfter=Jan 10 08:00:00 2027 GMT``. Spaces are collapsed first."""

    match = _ENDDATE.search(text)
    raw = match.group(1) if match else text
    collapsed = " ".join(raw.split())
    if collapsed.endswith(" GMT"):
        collapsed = collapsed[: -len(" GMT")]
    elif collapsed.endswith(" UTC"):
        collapsed = collapsed[: -len(" UTC")]
    try:
        parsed = datetime.strptime(collapsed, "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def days_remaining(not_after: datetime | None, now: datetime) -> int | None:
    if not_after is None:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    seconds = (not_after - moment.astimezone(UTC)).total_seconds()
    return int(seconds // 86400)


def name_covers(pattern: str, domain: str) -> bool:
    """Exact name, or a wildcard that covers exactly one label."""

    left = pattern.lower().rstrip(".")
    right = domain.lower().rstrip(".")
    if left == right:
        return True
    if not left.startswith("*."):
        return False
    suffix = left[2:]
    if not right.endswith("." + suffix):
        return False
    head = right[: -(len(suffix) + 1)]
    return bool(head) and "." not in head


class Tls:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        clock: Callable[[], datetime],
        le_live: Path = Path("/etc/letsencrypt/live"),
        custom_dir: Path = Path("/etc/ssl/keystone"),
        cron_file: Path = Path("/etc/cron.d/certbot"),
        http: httpx.Client | None = None,
        redact: Callable[[str], str] | None = None,
        is_root: bool | None = None,
        handshake: Callable[[str], dict] | None = None,
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.le_live = le_live
        self.custom_dir = custom_dir
        self.cron_file = cron_file
        self.http = http or httpx.Client(timeout=5.0)
        self.redact = redact or (lambda text: text)
        self._is_root = is_root
        self._handshake = handshake or _peer_cert
        self.warnings: list[dict[str, str]] = field_warnings()

    def _root(self) -> bool:
        if self._is_root is not None:
            return self._is_root
        return os.geteuid() == 0

    def _argv(self, args: Sequence[str]) -> list[str]:
        command = [str(item) for item in args]
        if self._root():
            return command
        return ["sudo", "-n", *command]

    def _run(
        self, args: Sequence[str], *, input: str | None = None, env: Mapping[str, str] | None = None
    ):
        merged = {"LC_ALL": "C"}
        if env:
            merged.update(env)
        return self.runner.run(self._argv(args), input=input, env=merged)

    def _warn(self, warning_id: str, message: str) -> None:
        self.warnings.append({"id": warning_id, "message": message})

    def _days(self, not_after: datetime | None) -> int | None:
        return days_remaining(not_after, self.clock())

    def _read_enddate(self, cert: str) -> datetime | None:
        result = self._run(["openssl", "x509", "-enddate", "-noout", "-in", cert])
        if result.returncode != 0:
            self.warnings.append(
                {"id": "unparsed_enddate", "message": "could not read the certificate end date"}
            )
            return None
        parsed = parse_enddate(result.stdout)
        if parsed is None:
            self.warnings.append(
                {"id": "unparsed_enddate", "message": f"could not parse notAfter for {cert}"}
            )
        return parsed

    def detect(self, domain: str) -> CertInfo:
        live_cert = self.le_live / domain / "fullchain.pem"
        live_key = self.le_live / domain / "privkey.pem"
        custom_cert = self.custom_dir / domain / "fullchain.pem"
        custom_key = self.custom_dir / domain / "privkey.pem"
        if self._exists(live_cert) and self._exists(live_key):
            return self._info("letsencrypt", live_cert, live_key)
        if self._exists(custom_cert) and self._exists(custom_key):
            return self._info("custom", custom_cert, custom_key)
        return CertInfo("none", None, None, None, None)

    def _exists(self, path: Path) -> bool:
        result = self._run(["test", "-f", str(path)])
        return result.returncode == 0

    def _info(self, kind: Literal["letsencrypt", "custom"], cert: Path, key: Path) -> CertInfo:
        not_after = self._read_enddate(str(cert))
        return CertInfo(kind, str(cert), str(key), not_after, self._days(not_after))

    def check_dns(self, domain: str) -> str:
        host_ip = self._public_ip()
        resolved = self._resolve(domain)
        if resolved is None:
            raise TlsError(
                "dns_no_record",
                f"{domain} has no A record",
                hint=f"add an A record {domain} → {host_ip}",
            )
        if resolved != host_ip:
            raise TlsError(
                "dns_mismatch",
                f"{domain} points to {resolved}, this host is {host_ip}",
                hint=(
                    "Update the A record or wait for DNS to propagate (often 1-10 min), "
                    "then run apply again."
                ),
            )
        return resolved

    def _public_ip(self) -> str:
        for url in _IP_URLS:
            try:
                response = self.http.get(url, timeout=5.0)
            except httpx.HTTPError:
                continue
            text = response.text.strip()
            if response.status_code == 200 and _IPV4.fullmatch(text):
                return text
        raise TlsError("public_ip_unknown", ERRORS["public_ip_unknown"].title)

    def _resolve(self, domain: str) -> str | None:
        dig = self.runner.run(
            ["dig", "+short", "A", domain, "@1.1.1.1"],
            env={"LC_ALL": "C"},
        )
        if dig.returncode == 0:
            lines = [line.strip() for line in dig.stdout.splitlines() if line.strip()]
            addresses = [line for line in lines if _IPV4.fullmatch(line)]
            return addresses[-1] if addresses else None
        self._warn(
            "dns_system_resolver",
            "dig is not available; the system resolver was used",
        )
        try:
            infos = socket.getaddrinfo(domain, None, socket.AF_INET)
        except socket.gaierror:
            return None
        for info in infos:
            address = info[4][0]
            if _IPV4.fullmatch(address):
                return address
        return None

    def issue_certbot(self, domain: str, email: str, *, staging: bool = False) -> CertInfo:
        version = self._run(["certbot", "--version"])
        if version.returncode != 0:
            raise TlsError(
                "certbot_missing",
                ERRORS["certbot_missing"].title,
                hint="install certbot and python3-certbot-nginx",
            )
        args = [
            "certbot",
            "--nginx",
            "-d",
            domain,
            "--redirect",
            "--non-interactive",
            "--agree-tos",
            "-m",
            email,
            "--no-eff-email",
        ]
        current = self.detect(domain)
        if (
            current.kind == "letsencrypt"
            and current.days_left is not None
            and current.days_left > 30
        ):
            args.append("--keep-until-expiring")
        if staging:
            args.append("--test-cert")
        result = self._run(args)
        if result.returncode != 0:
            tail = "\n".join((result.stderr or result.stdout).splitlines()[-20:])
            text = self.redact(tail)
            hint = _certbot_hint(text)
            raise TlsError("certbot_failed", text or ERRORS["certbot_failed"].title, hint=hint)
        return self.detect(domain)

    def install_custom(self, domain: str, cert: Path, key: Path) -> CertInfo:
        cert = Path(cert)
        key = Path(key)
        if self._run(["test", "-r", str(cert)]).returncode != 0:
            raise TlsError(
                "cert_unreadable",
                f"cannot read {cert}",
                hint="change KEYSTONE_SETUP_SERVICE_*_CERT_PATH",
            )
        if self._run(["test", "-r", str(key)]).returncode != 0:
            raise TlsError(
                "cert_unreadable",
                f"cannot read {key}",
                hint="change KEYSTONE_SETUP_SERVICE_*_KEY_PATH",
            )
        pem = self._run(["openssl", "x509", "-in", str(cert), "-noout"])
        if pem.returncode != 0:
            raise TlsError(
                "cert_not_pem",
                ERRORS["cert_not_pem"].title,
                hint=".pfx/.der? convert: openssl pkcs12 -in x.pfx -nokeys -out fullchain.pem",
            )
        self._chain_warning(cert)
        key_pub = self._run(["openssl", "pkey", "-in", str(key), "-pubout"])
        if key_pub.returncode != 0:
            raise TlsError(
                "key_not_pem",
                ERRORS["key_not_pem"].title,
                hint="change KEYSTONE_SETUP_SERVICE_*_KEY_PATH",
            )
        cert_pub = self._run(["openssl", "x509", "-in", str(cert), "-noout", "-pubkey"])
        if cert_pub.returncode != 0 or _normalize_pem(cert_pub.stdout) != _normalize_pem(
            key_pub.stdout
        ):
            raise TlsError(
                "key_mismatch",
                ERRORS["key_mismatch"].title,
                hint="change KEYSTONE_SETUP_SERVICE_*_KEY_PATH",
            )
        if (
            self._run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", "0"]).returncode
            != 0
        ):
            raise TlsError(
                "cert_expired",
                ERRORS["cert_expired"].title,
                hint="change KEYSTONE_SETUP_SERVICE_*_CERT_PATH",
            )
        if (
            self._run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-checkend", "2592000"]
            ).returncode
            != 0
        ):
            self._warn("cert_expiring", f"{cert} expires within 30 days")
        self._coverage_warning(cert, domain)
        destination = self.custom_dir / domain
        target_cert = destination / "fullchain.pem"
        target_key = destination / "privkey.pem"
        if not _same_file(cert, target_cert) or not _same_file(key, target_key):
            commands = (
                ["install", "-d", "-m", "700", "-o", "root", "-g", "root", str(destination)],
                [
                    "install",
                    "-m",
                    "644",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(cert),
                    str(target_cert),
                ],
                [
                    "install",
                    "-m",
                    "600",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(key),
                    str(target_key),
                ],
            )
            for command in commands:
                result = self._run(command)
                if result.returncode != 0:
                    raise TlsError(
                        "cert_install_failed",
                        self.redact(result.stderr or result.stdout or "install failed"),
                    )
        return self._info("custom", target_cert, target_key)

    def _chain_warning(self, cert: Path) -> None:
        counted = self._run(["grep", "-c", "BEGIN CERTIFICATE", str(cert)])
        try:
            count = int((counted.stdout or "0").strip() or "0")
        except ValueError:
            count = 0
        if count != 1:
            return
        names = self._run(["openssl", "x509", "-in", str(cert), "-noout", "-issuer", "-subject"])
        issuer = ""
        subject = ""
        for line in names.stdout.splitlines():
            if line.startswith("issuer="):
                issuer = line.split("=", 1)[1].strip()
            if line.startswith("subject="):
                subject = line.split("=", 1)[1].strip()
        if issuer and subject and issuer != subject:
            self._warn("chain_missing", "one certificate and the issuer differs from the subject")

    def _coverage_warning(self, cert: Path, domain: str) -> None:
        names = self._cert_names(cert)
        if names and any(name_covers(pattern, domain) for pattern in names):
            return
        self._warn("cert_may_not_cover_domain", f"{cert} may not cover {domain}")

    def _cert_names(self, cert: Path) -> list[str]:
        subject = self._run(["openssl", "x509", "-in", str(cert), "-noout", "-subject"])
        san = self._run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"])
        found: list[str] = []
        for line in subject.stdout.splitlines():
            if "CN" in line:
                piece = line.split("CN", 1)[1]
                piece = piece.lstrip(" =")
                token = piece.split(",", 1)[0].strip()
                if token:
                    found.append(token)
        found.extend(re.findall(r"DNS:([^,\s]+)", san.stdout))
        return found

    def renewal_check(self, domain: str) -> Renewal:
        info = self.detect(domain)
        if info.kind == "custom":
            self._warn(
                "renewal_manual", "renewal is on you; service status warns 30 days before expiry"
            )
            return Renewal("operator")
        if info.kind != "letsencrypt":
            return Renewal("missing")
        timers = self._run(["systemctl", "list-timers", "--all"])
        if timers.returncode == 0 and "certbot" in timers.stdout:
            return Renewal("systemd-timer")
        if self._exists(self.cron_file):
            return Renewal("cron")
        self._warn("renewal_timer_missing", "no certbot timer or cron file was found")
        return Renewal("missing")

    def remote_cert(self, domain: str) -> CertInfo:
        try:
            peer = self._handshake(domain)
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            raise TlsError("public_unreachable", str(exc)) from exc
        raw = peer.get("notAfter") if isinstance(peer, dict) else None
        not_after = parse_enddate(str(raw)) if raw else None
        return CertInfo("custom", None, None, not_after, self._days(not_after))


def field_warnings() -> list[dict[str, str]]:
    return []


def _normalize_pem(text: str) -> str:
    return "".join(text.split())


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve() and left.is_file() and right.is_file()
    except OSError:
        return False


def _certbot_hint(text: str) -> str:
    lowered = text.lower()
    if "no such authorization" in lowered or "malformed" in lowered:
        return "stale ACME order from an interrupted run; retry creates a new one"
    if "ratelimited" in lowered or "too many failed authorizations" in lowered:
        return "Let's Encrypt rate limit; wait an hour or use --certbot-staging"
    return "Check DNS, port 80, and /var/log/letsencrypt/letsencrypt.log, then run apply again."


def register(subparsers: object) -> None:
    """CLI hook. Public flags and lifecycle commands register from here."""

    from keystonectl.lifecycle import register_commands
    from keystonectl.public import register_commands as register_public

    register_public(subparsers)  # type: ignore[arg-type]
    register_commands(subparsers)  # type: ignore[arg-type]


def _peer_cert(domain: str) -> dict:
    context = ssl.create_default_context()
    with (
        socket.create_connection((domain, 443), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=domain) as wrapped,
    ):
        peer = wrapped.getpeercert()
    return dict(peer or {})
