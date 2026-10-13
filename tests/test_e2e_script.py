"""Static locks for the release-review fixes in scripts/e2e.sh."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "e2e.sh").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    lines = SCRIPT.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{name}()"))
    body: list[str] = []
    heredoc: str | None = None
    for line in lines[start + 1 :]:
        if heredoc is None and line == "}":
            break
        body.append(line)
        if heredoc is not None:
            if line == heredoc:
                heredoc = None
            continue
        if "<<" not in line:
            continue
        token = line.split("<<", 1)[1].strip()
        if token[:1] in {"'", '"'}:
            quote = token[0]
            token = token[1 : token.index(quote, 1)]
        else:
            token = token.split()[0]
        heredoc = token
    text = "\n".join(body)
    assert text, name
    return text


def test_external_s3_requires_the_installer_http_warning() -> None:
    body = _function_body("scenario_external_s3")
    assert "has_line s3_plain_http" in body
    assert "insecure_endpoint" not in body


def test_check_allows_installer_warnings_and_untrusted_tls() -> None:
    body = _function_body("scenario_check_clean")
    allowed = "tls_untrusted|s3_plain_http|postgres_plain_connection"
    assert allowed in body
    assert "insecure_endpoint" not in body
    assert "insecure_sslmode" not in body


def test_api_key_is_issued_for_every_profile() -> None:
    body = _function_body("main")
    assert "\n  issue_api_key\n" in f"\n{body}\n"
    assert 'if [[ "$PROFILE" == "ci" ]]; then\n    issue_api_key\n' not in body


def test_deploy_vm_skips_agent_path_missing_without_a_pass() -> None:
    body = _function_body("scenario_agent_path_missing")
    assert '[[ "$LAYOUT" == "keystone-deploy" && "$PROFILE" == "vm" ]]' in body
    skip = 'skip agent_path_missing "setup.sh already installed bundle minimal"'
    assert skip in body
    assert body.index(skip) < body.index("kc bundle install minimal")
    assert body.index("return 0") < body.index("pass agent_path_missing")


def test_vm_manual_tls_starts_caddy_from_the_production_example() -> None:
    starter = _function_body("start_manual_caddy")
    assert "sudo" not in starter
    assert '"$ROOT/Caddyfile"' in starter
    assert "caddy run --config" in starter
    assert "handle /u/*" in starter
    assert "handle /d/*" in starter
    assert "handle /healthz" in starter
    assert "respond 404" in starter
    scenario = _function_body("scenario_tls_manual")
    assert 'start_manual_caddy || fail tls_manual "could not start Caddy"' in scenario
    assert "pass tls_manual" in scenario
    before_apply = scenario.split('"$PROFILE" == "ci"', 1)[1].split(
        "run_answered service apply file-mcp", 1
    )[0]
    assert "return 0" not in before_apply
