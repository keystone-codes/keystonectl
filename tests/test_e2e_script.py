"""Static locks for the release-review fixes in scripts/e2e.sh."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "e2e.sh").read_text(encoding="utf-8")
README = (ROOT / "scripts" / "e2e" / "README.md").read_text(encoding="utf-8")


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


def test_override_scenario_upgrades_real_file_mcp() -> None:
    body = _function_body("scenario_override_survives_upgrade")
    assert "0.9.0" not in body
    assert "service upgrade file-mcp" in body


def test_vm_manual_tls_uses_the_nginx_vhost_not_caddy() -> None:
    assert "start_manual_caddy" not in SCRIPT
    assert "caddy run" not in SCRIPT
    scenario = _function_body("scenario_tls_manual")
    assert (
        'install_manual_vhost || fail tls_manual "could not install the sample vhost"' in scenario
    )
    assert 'export SSL_CERT_FILE="$WORK/tls/cert.pem"' in scenario
    assert "pass tls_manual" in scenario
    # Both profiles take the same path: no profile branch left before the
    # second apply.
    before_apply = scenario.split("status_file_mcp ", 1)[1].split(
        "run_answered service apply e2e/file-mcp", 1
    )[0]
    assert '"$PROFILE"' not in before_apply


def test_manual_vhost_is_shared_by_both_profiles_and_uses_the_run_domain() -> None:
    body = _function_body("install_manual_vhost")
    assert "ensure_self_signed_cert" in body
    assert "sudo" in body
    assert '"$DOMAIN"' in body


def test_pypi_spec_prepares_a_cert_for_the_actual_domain() -> None:
    body = _function_body("ensure_self_signed_cert")
    assert "/CN=${DOMAIN}" in body
    assert "subjectAltName=DNS:${DOMAIN}" in body


def test_certbot_runs_before_manual_tls() -> None:
    body = _function_body("main")
    assert body.index("scenario_tls_certbot") < body.index("scenario_tls_manual")


def test_certbot_reissue_is_checked_by_serial_not_log_size() -> None:
    body = _function_body("scenario_tls_certbot")
    assert "x509 -noout -serial" in body
    assert "letsencrypt.log" not in body
    assert "wc -c" not in body


def test_vm_profile_checks_pypi_before_installing() -> None:
    lines = SCRIPT.splitlines()
    pyproject_index = next(
        index
        for index, line in enumerate(lines)
        if "PYPROJECT_VERSION=" in line and "tomllib" in line
    )
    spec_default_index = next(
        index
        for index, line in enumerate(lines)
        if 'KEYSTONECTL_SPEC="pypi:${PYPROJECT_VERSION}"' in line
    )
    guard_index = next(
        index
        for index, line in enumerate(lines)
        if 'echo "keystonectl $pypi_version is not on PyPI:' in line
    )
    main_index = next(index for index, line in enumerate(lines) if line.startswith("main()"))
    assert pyproject_index < spec_default_index < guard_index < main_index
    guard_block = "\n".join(lines[guard_index - 3 : guard_index + 2])
    assert 'KEYSTONECTL_SPEC" == pypi:*' in guard_block
    assert "pypi_version_exists" in guard_block
    assert "exit 1" in guard_block
    assert "pass --keystonectl <wheel or checkout>" in guard_block


def test_vm_profile_requires_domain() -> None:
    env = os.environ.copy()
    env["E2E_PYPI_CHECK_CMD"] = "true"
    env.pop("E2E_TLS_EMAIL", None)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "e2e.sh"),
            "--profile",
            "vm",
            "--tls",
            "certbot",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "--domain is required for the vm profile" in result.stderr


def test_vm_certbot_requires_real_email() -> None:
    env = os.environ.copy()
    env["E2E_PYPI_CHECK_CMD"] = "true"
    base_cmd = [
        "bash",
        str(ROOT / "scripts" / "e2e.sh"),
        "--profile",
        "vm",
        "--tls",
        "certbot",
        "--domain",
        "files.example.com",
    ]
    for email in (None, "admin@example.com"):
        run_env = env.copy()
        if email is None:
            run_env.pop("E2E_TLS_EMAIL", None)
        else:
            run_env["E2E_TLS_EMAIL"] = email
        result = subprocess.run(
            base_cmd,
            cwd=ROOT,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "set E2E_TLS_EMAIL to a real address" in result.stderr


def test_vm_default_spec_follows_pyproject_version() -> None:
    expected = tomllib.load((ROOT / "pyproject.toml").open("rb"))["project"]["version"]
    assert 'KEYSTONECTL_SPEC="pypi:${PYPROJECT_VERSION}"' in SCRIPT
    assert "tomllib.load(open(sys.argv[1]" in SCRIPT
    read_version = subprocess.run(
        [
            "python3",
            "-c",
            'import tomllib, sys; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])',
            str(ROOT / "pyproject.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert read_version.stdout.strip() == expected


_MATRIX_COLUMNS = (
    "production-example ci",
    "production-example vm",
    "keystone-deploy ci",
    "keystone-deploy vm",
)

_SKIP_RULES: dict[str, list[tuple[str, str]]] = {
    "scenario_agent_path_missing": [
        (
            '[[ "$LAYOUT" == "keystone-deploy" && "$PROFILE" == "vm" ]]',
            "setup.sh already installed bundle minimal",
        ),
    ],
    "scenario_tls_certbot": [
        ('[[ "$PROFILE" != "vm" || "$TLS" != "certbot" ]]', "certbot runs only on the vm profile"),
    ],
    "scenario_tls_manual": [
        ('[[ "$LAYOUT" != "production-example" ]]', "only the production example"),
    ],
}


def _layout_profile_tls(column: str) -> tuple[str, str, str]:
    layout, profile = column.rsplit(" ", 1)
    tls = "certbot" if profile == "vm" else "custom"
    return layout, profile, tls


def _condition_applies(condition: str, layout: str, profile: str, tls: str) -> bool:
    if condition == '[[ "$LAYOUT" == "keystone-deploy" && "$PROFILE" == "vm" ]]':
        return layout == "keystone-deploy" and profile == "vm"
    if condition == '[[ "$PROFILE" != "vm" || "$TLS" != "certbot" ]]':
        return profile != "vm" or tls != "certbot"
    if condition == '[[ "$LAYOUT" != "production-example" ]]':
        return layout != "production-example"
    raise AssertionError(f"unknown matrix condition: {condition}")


def _parse_scenario_matrix() -> list[tuple[str, str, dict[str, str | None]]]:
    section = README.split("### Scenario matrix", 1)[1]
    table_lines = [
        line
        for line in section.splitlines()
        if line.startswith("|")
        and not line.startswith("| Scenario")
        and not line.startswith("|---")
    ]
    rows: list[tuple[str, str, dict[str, str | None]]] = []
    for line in table_lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        label, *values = cells
        assert len(values) == len(_MATRIX_COLUMNS)
        parsed: dict[str, str | None] = {}
        for column, cell in zip(_MATRIX_COLUMNS, values, strict=True):
            if cell == "runs":
                parsed[column] = None
            else:
                match = re.fullmatch(r"skipped \((.+)\)", cell)
                assert match, f"unexpected cell {cell!r} in {label}"
                parsed[column] = match.group(1)
        match = re.search(r"`([^`]+)`", label)
        assert match, label
        rows.append((f"scenario_{match.group(1)}", label, parsed))
    return rows


def test_scenario_matrix_matches_skips() -> None:
    for function_name, _label, cells in _parse_scenario_matrix():
        rules = _SKIP_RULES.get(function_name, [])
        body = _function_body(function_name)
        skip_token = function_name.removeprefix("scenario_")
        for condition, reason in rules:
            assert f'skip {skip_token} "{reason}"' in body
            assert condition in body
        for column, expected_reason in cells.items():
            layout, profile, tls = _layout_profile_tls(column)
            triggered = [
                reason
                for condition, reason in rules
                if _condition_applies(condition, layout, profile, tls)
            ]
            if expected_reason is None:
                assert triggered == [], (function_name, column, triggered)
            else:
                assert triggered == [expected_reason], (function_name, column, triggered)


def test_pypi_version_exists_defers_to_the_check_command_override() -> None:
    # The default path would hit the real PyPI index; substitute the check
    # command via E2E_PYPI_CHECK_CMD so this test never touches the network.
    body = _function_body("pypi_version_exists")
    assert "E2E_PYPI_CHECK_CMD" in body
    assert "curl" in body
    assert "command -v curl" in body

    script = f'pypi_version_exists() {{\n{body}\n}}\npypi_version_exists "$1"\n'

    env = os.environ.copy()
    env["E2E_PYPI_CHECK_CMD"] = "false"
    absent = subprocess.run(
        ["bash", "-c", script, "bash", "9.9.9-not-on-pypi"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert absent.returncode != 0

    env["E2E_PYPI_CHECK_CMD"] = "true"
    present = subprocess.run(
        ["bash", "-c", script, "bash", "0.1.0"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert present.returncode == 0


def test_production_example_env_does_not_preset_allow_insecure_http() -> None:
    body = _function_body("prepare_production_example")
    assert "KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP" not in body


def test_install_scenario_asserts_keystonectl_set_allow_insecure_http() -> None:
    body = _function_body("scenario_install_file_mcp_minio")
    assert "KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP" in body
    assert "managed block, do not edit" in body


def test_partner_catalog_uses_url_when_set() -> None:
    body = _function_body("scenario_partner_catalog")
    assert 'catalog add "$E2E_PARTNER_CATALOG_URL" --ref' in body
    url_branch, local_branch = body.split("else", 1)
    assert (
        'kc catalog add "$E2E_PARTNER_CATALOG_URL" --ref "$E2E_PARTNER_CATALOG_REF"' in url_branch
    )
    assert "--name" not in url_branch
    assert 'fail partner_catalog "local_source was warned"' in url_branch
    assert 'fail partner_catalog "catalog_ref_chosen was warned"' in url_branch
    assert (
        'note "partner catalog ${E2E_PARTNER_CATALOG_URL} ref ${E2E_PARTNER_CATALOG_REF}"'
        in url_branch
    )
    assert 'kc catalog add "file://${WORK}/partner-catalog"' in local_branch
    assert "local_source was not warned" in local_branch
    assert 'note "partner catalog file://"' in local_branch


def test_vm_profile_defaults_to_network_partner_catalog() -> None:
    assert 'E2E_PARTNER_CATALOG_REF="${E2E_PARTNER_CATALOG_REF:-v1.0.0}"' in SCRIPT
    marker = (
        'if [[ "$PROFILE" == "vm" ]]; then\n'
        '    E2E_PARTNER_CATALOG_URL="https://github.com/keystone-codes/e2e-partner-catalog.git"'
    )
    assert marker in SCRIPT
    assert 'if [[ -z "${E2E_PARTNER_CATALOG_URL+x}" ]]; then' in SCRIPT


def test_legacy_gone_runs_on_both_layouts() -> None:
    body = _function_body("scenario_legacy_gone")
    assert 'skip legacy_gone "only keystone-deploy"' not in body
    assert '"$LAYOUT" == "keystone-deploy"' in body
    assert '"$LAYOUT" == "production-example"' in body


def test_operator_snapshot_taken_after_publish_port_rewrite() -> None:
    body = _function_body("prepare_production_example")
    assert body.index("rewrite_publish_port") < body.index("write_operator_snapshot")
    assert "operator.sha" in _function_body("write_operator_snapshot")


def test_leftover_check_covers_tmp_files_and_packages_dir() -> None:
    body = _function_body("scenario_legacy_gone")
    assert ".*.tmp" in body
    assert '.keystonectl" / "tmp' in body
    assert "packages/" in body
    assert "kept.txt" in body


def test_atlassian_profiles_is_wired_into_main() -> None:
    body = _function_body("main")
    assert body.index("scenario_partner_catalog") < body.index("scenario_atlassian_profiles")
    assert body.index("scenario_legacy_gone") < body.index("scenario_atlassian_profiles")
    assert "path: packages/atlassian" in _function_body("scenario_atlassian_profiles")
    assert "path: packages/atlassian" in _function_body("build_local_catalogs")
    assert re.search(
        r"STDIO_BOX_IMAGE=ghcr.io/keystone-codes/stdio-box:0\.1\.0@sha256:[0-9a-f]{64}",
        SCRIPT,
    )
    assert "16. `atlassian_profiles`" in README
    assert "| 16 `atlassian_profiles` | runs | runs | runs | runs |" in README


def test_atlassian_check_hides_secrets_and_creds_tokens() -> None:
    import importlib.util

    path = ROOT / "scripts" / "e2e" / "atlassian_check.py"
    text = path.read_text(encoding="utf-8")
    assert "/creds/***" in text
    for line in text.splitlines():
        if "print(" in line or "json.dump" in line:
            assert "E2E_ATL_" not in line
    spec = importlib.util.spec_from_file_location("e2e_atlassian_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shown = module.host_path("https://atlassian.e2e.test/creds/super-secret-token?x=1")
    assert shown == "atlassian.e2e.test/creds/***"
    assert "super-secret-token" not in shown
    assert module._leaks({"steps": [{"form": "https://host/creds/tok"}]}, {})
    assert not module._leaks({"steps": [{"form": "atlassian.e2e.test/creds/***"}]}, {})
    assert module._leaks(
        {"steps": [{"note": "secret-value"}]}, {"E2E_ATL_DEV_TOKEN": "secret-value"}
    )


def test_ci_profile_keeps_local_partner_catalog() -> None:
    vm_url = SCRIPT.index(
        'E2E_PARTNER_CATALOG_URL="https://github.com/keystone-codes/e2e-partner-catalog.git"'
    )
    empty_url = SCRIPT.index('E2E_PARTNER_CATALOG_URL=""')
    assert vm_url < empty_url
    body = _function_body("scenario_partner_catalog")
    assert '[[ -n "$E2E_PARTNER_CATALOG_URL" ]]' in body
    local_branch = body.split("else", 1)[1]
    assert "file://${WORK}/partner-catalog" in local_branch
    assert "github.com/keystone-codes/e2e-partner-catalog" not in body
