"""Atlassian profile checks for the e2e script. Runs inside the keystone-mcp image.

API keys and tokens are read from E2E_ATL_* variables. They are never printed.
A form URL is reported only as its host plus ``/creds/***``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

MOCK_URL = "http://atlassian-mock:8080"
ORIGIN = "https://atlassian.e2e.test"
DEV_PROFILE = "atlassian/dev-team"
DESIGN_PROFILE = "atlassian/design"
BACKEND_PROFILE = "atlassian/backend"
PROFILE_IDS = (DEV_PROFILE, DESIGN_PROFILE, BACKEND_PROFILE)
_CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
_SECRET_NAMES = (
    "E2E_ATL_ADMIN_KEY",
    "E2E_ATL_DEV_KEY",
    "E2E_ATL_DESIGN_KEY",
    "E2E_ATL_BOTH_KEY",
    "E2E_ATL_NOBODY_KEY",
    "E2E_ATL_BACKEND_KEY",
    "E2E_ATL_DEV_TOKEN",
    "E2E_ATL_DESIGN_TOKEN",
    "E2E_ATL_BACKEND_TOKEN",
)


def host_path(url: str) -> str:
    """Host and path only. A ``/creds/`` token is replaced with ``***``."""

    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    path = parsed.path or ""
    marker = "/creds/"
    if marker in path:
        path = path.split(marker, 1)[0] + "/creds/***"
    return f"{host}{path}"


def pat_name(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"pat-{digest}"


def report(phase: str, steps: list[dict[str, Any]], *, ok: bool) -> dict[str, Any]:
    return {"ok": ok, "phase": phase, "steps": steps}


class CheckError(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _payload(result: Any) -> Any:
    data = getattr(result, "data", None)
    if isinstance(data, (dict, list, str)):
        return _decode(data)
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if isinstance(text, str) and text.strip():
                return _decode(text)
    if isinstance(result, (dict, list, str)):
        return _decode(result)
    raise CheckError("client", "tool result had no JSON payload")


def _refusal_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, str) and "message" in payload and "hint" in payload:
        return code
    return None


def _interpret(result: Any) -> tuple[Any, str | None]:
    payload = _payload(result)
    code = _refusal_code(payload)
    flagged = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    if flagged or code is not None:
        return payload, code
    return payload, None


def _client(url: str, key: str) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {key}"})
    try:
        return Client(transport, timeout=180)
    except TypeError:
        return Client(transport)


async def _raw_call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    attempts: tuple[dict[str, Any], ...] = (
        {"raise_on_error": False, "timeout": 180},
        {"timeout": 180},
        {},
    )
    for kwargs in attempts:
        try:
            return await client.call_tool(name, arguments, **kwargs)
        except TypeError:
            continue
    raise CheckError(name, "call_tool")


async def invoke(client: Any, name: str, arguments: dict[str, Any]) -> tuple[Any, str | None]:
    try:
        result = await _raw_call(client, name, arguments)
    except CheckError:
        raise
    except Exception as exc:  # noqa: BLE001 — the message can contain a token or a form URL
        code = _refusal_code(_decode(str(exc)))
        if code is not None:
            return {"code": code, "message": "", "hint": ""}, code
        raise CheckError(name, type(exc).__name__) from None
    return _interpret(result)


def _require_code(code: str | None, expected: str, step: str) -> None:
    if code != expected:
        raise CheckError(step, code or "missing code")


def _seen(payload: Any, code: str | None, needle: str, step: str) -> None:
    if code:
        raise CheckError(step, code)
    if needle not in json.dumps(payload, ensure_ascii=False):
        raise CheckError(step, "identity mismatch")


def _object(payload: Any, code: str | None, step: str) -> dict[str, Any]:
    if code:
        raise CheckError(step, code)
    if not isinstance(payload, dict):
        raise CheckError(step, "not an object")
    return payload


def _profiles(payload: Any, code: str | None) -> dict[str, dict[str, Any]]:
    document = _object(payload, code, "profiles")
    rows = document.get("profiles")
    if not isinstance(rows, list):
        raise CheckError("profiles", "not a list")
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            found[row["id"]] = row
    return found


def _require_ready(found: dict[str, dict[str, Any]]) -> None:
    for profile_id in PROFILE_IDS:
        row = found.get(profile_id)
        if row is None or row.get("state") != "ready":
            raise CheckError("profiles", "profile not ready")


def _bindings(payload: Any, code: str | None) -> dict[str, Any]:
    return _object(payload, code, "bindings")


def _require_order(document: dict[str, Any]) -> None:
    role = document.get("role")
    user = document.get("user")
    if not isinstance(role, list) or len(role) < 2:
        raise CheckError("bindings", "role order")
    first, second = role[0], role[1]
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise CheckError("bindings", "role order")
    if first.get("position") != 1 or first.get("profile") != DEV_PROFILE:
        raise CheckError("bindings", "dev position")
    if second.get("position") != 2 or second.get("profile") != DESIGN_PROFILE:
        raise CheckError("bindings", "design position")
    if not isinstance(user, list) or not any(
        isinstance(item, dict)
        and item.get("subject") == "user:svc:e2e-atl-backend"
        and item.get("profile") == BACKEND_PROFILE
        for item in user
    ):
        raise CheckError("bindings", "backend binding")


def _form_fields(variant: str, token: str) -> dict[str, str]:
    if variant == "server":
        return {
            "JIRA_PERSONAL_TOKEN": token,
            "CONFLUENCE_PERSONAL_TOKEN": token,
        }
    return {"JIRA_PERSONAL_TOKEN": token}


def _submit_form(url: str, ca: str, fields: dict[str, str], identity: str) -> str:
    import httpx

    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "atlassian.e2e.test" or not (parsed.path or "").startswith("/creds/"):
        raise CheckError("form_get", "form host")
    with httpx.Client(verify=ca, timeout=60.0, follow_redirects=False) as http:
        got = http.get(url)
        if got.status_code != 200:
            raise CheckError("form_get", f"HTTP {got.status_code}")
        match = _CSRF_RE.search(got.text)
        if match is None:
            raise CheckError("form_get", "csrf field missing")
        posted = http.post(
            url,
            data={"csrf": match.group(1), **fields},
            headers={"Origin": ORIGIN},
        )
        if posted.status_code != 200:
            raise CheckError("form_post", f"HTTP {posted.status_code}")
        if identity not in posted.text:
            raise CheckError("form_post", "identity name missing")
        again = http.get(url)
        if again.status_code != 404:
            raise CheckError("form_reuse", f"HTTP {again.status_code}")
    return host_path(url)


async def _create(
    client: Any,
    *,
    name: str,
    variant: str,
    fields: dict[str, str],
    token: str,
    form: bool,
    ca: str,
    step: str,
) -> dict[str, Any]:
    payload, code = await invoke(
        client,
        "atlassian_creds_profile_create",
        {"name": name, "variant": variant, "fields": fields},
    )
    if form:
        document = _object(payload, code, step)
        form_url = document.get("form_url")
        if not isinstance(form_url, str):
            raise CheckError(step, "form_url missing")
        shown = _submit_form(form_url, ca, _form_fields(variant, token), pat_name(token))
        return {"name": step, "ok": True, "form": shown}
    _require_code(code, "form_unavailable", step)
    return {"name": step, "ok": True, "code": "form_unavailable"}


async def _providers(client: Any) -> tuple[dict[str, Any], str]:
    payload, code = await invoke(client, "atlassian_creds_providers", {})
    document = _object(payload, code, "providers")
    providers = document.get("providers")
    if not isinstance(providers, list) or len(providers) != 1 or not isinstance(providers[0], dict):
        raise CheckError("providers", "provider list")
    provider = providers[0]
    variants = provider.get("variants")
    server = provider.get("server")
    installed = server.get("installed_key") if isinstance(server, dict) else None
    if not isinstance(variants, list) or len(variants) != 6:
        raise CheckError("providers", "variant count")
    if not isinstance(installed, str) or not installed or installed == "unknown":
        raise CheckError("providers", "installed_key empty")
    return {"name": "providers", "ok": True, "variants": 6, "installed_key": installed}, installed


async def _bind(client: Any, subject: str, profile: str, **extra: Any) -> None:
    arguments: dict[str, Any] = {"subject": subject, "profile": profile}
    arguments.update(extra)
    _payload_value, code = await invoke(client, "atlassian_creds_bind", arguments)
    if code:
        raise CheckError("bind", code)


async def run_setup(url: str, secrets: dict[str, str], *, form: bool, ca: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    async with _client(url, secrets["E2E_ATL_ADMIN_KEY"]) as admin:
        providers_step, _installed = await _providers(admin)
        steps.append(providers_step)
        _payload_value, code = await invoke(
            admin,
            "atlassian_creds_profile_create",
            {
                "name": "dev-team",
                "variant": "jira_cloud",
                "fields": {"JIRA_URL": MOCK_URL, "JIRA_USERNAME": "dev@e2e.test"},
            },
        )
        _require_code(code, "invalid_argument", "reject_cloud_url")
        steps.append({"name": "reject_cloud_url", "ok": True, "code": "invalid_argument"})
        public = {"JIRA_URL": MOCK_URL, "CONFLUENCE_URL": MOCK_URL}
        steps.append(
            await _create(
                admin,
                name="dev-team",
                variant="server",
                fields=public,
                token=secrets["E2E_ATL_DEV_TOKEN"],
                form=form,
                ca=ca,
                step="profile_dev",
            )
        )
        jira_only = {"JIRA_URL": MOCK_URL}
        steps.append(
            await _create(
                admin,
                name="design",
                variant="jira_server",
                fields=jira_only,
                token=secrets["E2E_ATL_DESIGN_TOKEN"],
                form=form,
                ca=ca,
                step="profile_design",
            )
        )
        steps.append(
            await _create(
                admin,
                name="backend",
                variant="jira_server",
                fields=jira_only,
                token=secrets["E2E_ATL_BACKEND_TOKEN"],
                form=form,
                ca=ca,
                step="profile_backend",
            )
        )
        await _bind(admin, "role:atlassian_dev", DEV_PROFILE)
        await _bind(admin, "role:atlassian_design", DESIGN_PROFILE)
        await _bind(admin, "user:svc:e2e-atl-backend", BACKEND_PROFILE)
        payload, code = await invoke(admin, "atlassian_creds_bindings", {})
        _require_order(_bindings(payload, code))
        steps.append({"name": "bindings", "ok": True})
    return report("setup", steps, ok=True)


async def _whoami(client: Any, step: str) -> dict[str, Any]:
    payload, code = await invoke(client, "atlassian_creds_whoami", {})
    return _object(payload, code, step)


async def _issue(client: Any, token: str, step: str) -> dict[str, Any]:
    payload, code = await invoke(
        client,
        "atlassian_jira_get_issue",
        {"issue_key": "E2E-1", "fields": "summary", "comment_limit": 0},
    )
    needle = f"issue seen by {pat_name(token)}"
    _seen(payload, code, needle, step)
    return {"name": step, "ok": True, "identity": pat_name(token)}


async def run_calls(url: str, secrets: dict[str, str]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    dev_pat = pat_name(secrets["E2E_ATL_DEV_TOKEN"])
    design_pat = pat_name(secrets["E2E_ATL_DESIGN_TOKEN"])
    async with (
        _client(url, secrets["E2E_ATL_DEV_KEY"]) as dev,
        _client(url, secrets["E2E_ATL_DESIGN_KEY"]) as design,
        _client(url, secrets["E2E_ATL_BOTH_KEY"]) as both,
        _client(url, secrets["E2E_ATL_BACKEND_KEY"]) as backend,
        _client(url, secrets["E2E_ATL_NOBODY_KEY"]) as nobody,
        _client(url, secrets["E2E_ATL_ADMIN_KEY"]) as admin,
    ):
        who = await _whoami(dev, "whoami_dev")
        if who.get("profile") != DEV_PROFILE or who.get("tier") != "role":
            raise CheckError("whoami_dev", "profile")
        binding = who.get("binding")
        if not isinstance(binding, dict) or binding.get("position") != 1:
            raise CheckError("whoami_dev", "position")
        steps.append({"name": "whoami_dev", "ok": True, "profile": DEV_PROFILE, "tier": "role"})
        steps.append(await _issue(dev, secrets["E2E_ATL_DEV_TOKEN"], "jira_dev"))
        payload, code = await invoke(
            dev,
            "atlassian_confluence_get_page",
            {"page_id": "1", "convert_to_markdown": False},
        )
        _seen(payload, code, f"page seen by {dev_pat}", "confluence_dev")
        steps.append({"name": "confluence_dev", "ok": True, "identity": dev_pat})

        steps.append(await _issue(design, secrets["E2E_ATL_DESIGN_TOKEN"], "jira_design"))
        _payload_value, code = await invoke(
            design,
            "atlassian_confluence_get_page",
            {"page_id": "1", "convert_to_markdown": False},
        )
        _require_code(code, "tool_not_in_profile", "confluence_design")
        steps.append({"name": "confluence_design", "ok": True, "code": "tool_not_in_profile"})

        who = await _whoami(both, "whoami_both")
        if who.get("profile") != DEV_PROFILE:
            raise CheckError("whoami_both", "profile")
        unused = who.get("not_used")
        if not isinstance(unused, list) or not any(
            isinstance(item, dict) and item.get("subject") == "role:atlassian_design"
            for item in unused
        ):
            raise CheckError("whoami_both", "not_used")
        steps.append({"name": "whoami_both", "ok": True, "profile": DEV_PROFILE})
        await _bind(
            admin,
            "role:atlassian_design",
            DESIGN_PROFILE,
            replace=True,
            position=1,
        )
        payload, code = await invoke(admin, "atlassian_creds_bindings", {})
        document = _bindings(payload, code)
        role = document.get("role")
        if (
            not isinstance(role, list)
            or not isinstance(role[0], dict)
            or role[0].get("profile") != DESIGN_PROFILE
            or role[0].get("position") != 1
        ):
            raise CheckError("bindings_reordered", "design position")
        steps.append({"name": "bindings_reordered", "ok": True})
        steps.append(await _issue(both, secrets["E2E_ATL_DESIGN_TOKEN"], "jira_both"))
        await _bind(admin, "role:atlassian_dev", DEV_PROFILE, replace=True, position=1)
        payload, code = await invoke(admin, "atlassian_creds_bindings", {})
        _require_order(_bindings(payload, code))
        steps.append({"name": "bindings_restored", "ok": True})

        who = await _whoami(backend, "whoami_backend")
        if who.get("sub_type") != "service" or who.get("tier") != "user":
            raise CheckError("whoami_backend", "tier")
        steps.append({"name": "whoami_backend", "ok": True, "sub_type": "service", "tier": "user"})
        steps.append(await _issue(backend, secrets["E2E_ATL_BACKEND_TOKEN"], "jira_backend"))

        _payload_value, code = await invoke(
            nobody,
            "atlassian_jira_get_issue",
            {"issue_key": "E2E-1"},
        )
        _require_code(code, "no_credentials_profile", "jira_nobody")
        steps.append({"name": "jira_nobody", "ok": True, "code": "no_credentials_profile"})

        payload, code = await invoke(admin, "atlassian_creds_profiles", {})
        found = _profiles(payload, code)
        _require_ready(found)
        slots: list[Any] = []
        for profile_id in PROFILE_IDS:
            instance = found[profile_id].get("instance")
            if not isinstance(instance, dict) or instance.get("state") != "running":
                raise CheckError("profiles", "instance not running")
            slots.append(instance.get("slot"))
        if len(set(slots)) != 3:
            raise CheckError("profiles", "instance slots")
        steps.append({"name": "profiles", "ok": True, "ready": 3})
        _payload_value, code = await invoke(
            admin,
            "atlassian_creds_profile_delete",
            {"profile": DEV_PROFILE},
        )
        _require_code(code, "profile_in_use", "delete_in_use")
        steps.append({"name": "delete_in_use", "ok": True, "code": "profile_in_use"})
    if design_pat == dev_pat:
        raise CheckError("tokens", "identities collided")
    return report("calls", steps, ok=True)


def _from_cache(path: str) -> None:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError("from_cache", type(exc).__name__) from None
    result = document.get("result") if isinstance(document.get("result"), dict) else document
    install = result.get("install") if isinstance(result, dict) else None
    if not isinstance(install, dict) or install.get("from_cache") is not True:
        raise CheckError("from_cache", "install.from_cache is not true")


async def run_reinstalled(
    url: str,
    secrets: dict[str, str],
    *,
    installed_key: str,
    box_status: str,
) -> dict[str, Any]:
    if not installed_key:
        raise CheckError("providers", "installed_key empty")
    if not box_status:
        raise CheckError("from_cache", "status missing")
    steps: list[dict[str, Any]] = []
    async with (
        _client(url, secrets["E2E_ATL_ADMIN_KEY"]) as admin,
        _client(url, secrets["E2E_ATL_DEV_KEY"]) as dev,
    ):
        payload, code = await invoke(admin, "atlassian_creds_profiles", {})
        _require_ready(_profiles(payload, code))
        steps.append({"name": "profiles", "ok": True, "ready": 3})
        payload, code = await invoke(admin, "atlassian_creds_bindings", {})
        _require_order(_bindings(payload, code))
        steps.append({"name": "bindings", "ok": True})
        steps.append(await _issue(dev, secrets["E2E_ATL_DEV_TOKEN"], "jira_dev"))
        _providers_step, installed = await _providers(admin)
        if installed != installed_key:
            raise CheckError("providers", "installed_key changed")
        steps.append(_providers_step)
    _from_cache(box_status)
    steps.append({"name": "from_cache", "ok": True})
    return report("reinstalled", steps, ok=True)


def load_secrets() -> dict[str, str]:
    found: dict[str, str] = {}
    for name in _SECRET_NAMES:
        value = os.environ.get(name, "")
        if not value:
            raise CheckError("auth", "required secret is not set")
        found[name] = value
    return found


def _leaks(document: dict[str, Any], secrets: dict[str, str]) -> bool:
    raw = json.dumps(document)
    if any(value and value in raw for value in secrets.values()):
        return True
    masked = raw.replace("/creds/***", "")
    return "/creds/" in masked


def _emit(document: dict[str, Any], secrets: dict[str, str]) -> int:
    if secrets and _leaks(document, secrets):
        document = report(
            str(document.get("phase") or ""),
            [{"name": "report", "ok": False, "error": "secret in report"}],
            ok=False,
        )
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if document.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check atlassian profiles from inside e2e")
    parser.add_argument("--keystone", required=True)
    parser.add_argument("--ca", default="")
    parser.add_argument("--phase", required=True, choices=("setup", "calls", "reinstalled"))
    parser.add_argument("--form", required=True, choices=("true", "false"))
    parser.add_argument("--expect-installed-key", default="")
    parser.add_argument("--box-status", default="")
    return parser


def _endpoint(base: str) -> str:
    text = base.strip()
    if not text.endswith("/"):
        text += "/"
    return text


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secrets: dict[str, str] = {}
    try:
        secrets = load_secrets()
        if args.form == "true" and not args.ca:
            raise CheckError("form_get", "CA is not set")
        url = _endpoint(args.keystone)
        form = args.form == "true"
        if args.phase == "setup":
            document = asyncio.run(run_setup(url, secrets, form=form, ca=args.ca))
        elif args.phase == "calls":
            document = asyncio.run(run_calls(url, secrets))
        else:
            document = asyncio.run(
                run_reinstalled(
                    url,
                    secrets,
                    installed_key=args.expect_installed_key,
                    box_status=args.box_status,
                )
            )
    except CheckError as exc:
        document = report(
            args.phase, [{"name": exc.step, "ok": False, "error": exc.message}], ok=False
        )
        return _emit(document, secrets)
    except Exception as exc:  # noqa: BLE001 — the message can contain a token or a form URL
        document = report(
            args.phase,
            [{"name": "client", "ok": False, "error": type(exc).__name__}],
            ok=False,
        )
        return _emit(document, secrets)
    return _emit(document, secrets)


if __name__ == "__main__":
    raise SystemExit(main())
