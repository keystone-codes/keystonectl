"""MCP client for the e2e script. Runs inside the keystone-mcp image.

The API key is read from E2E_API_KEY. It is never printed. URLs in the
report keep only the host and the path; the query string (the token) is dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import urllib.parse
from collections.abc import Callable
from typing import Any

MIB = 1024 * 1024
FILES_UPLOAD_CONTENT_TYPE = "application/octet-stream"


def host_path(url: str) -> str:
    """Host and path only. Query, fragment, and userinfo stay out of the report."""

    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    return f"{host}{parsed.path}"


def mcp_endpoint(base: str) -> str:
    text = base.strip()
    if not text.endswith("/"):
        text += "/"
    return text


def tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name is None and isinstance(tool, dict):
            name = tool.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def payload_of(result: Any) -> Any:
    """Normalise a fastmcp call result to the tool's JSON value."""

    data = getattr(result, "data", None)
    if isinstance(data, (dict, list, str)):
        return _decode(data)
    structured = getattr(result, "structured_content", None)
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
    raise ValueError("tool result had no JSON payload")


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def whoami_sub(payload: Any) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("sub"), str):
        return payload["sub"]
    if isinstance(payload, str):
        decoded = _decode(payload)
        if isinstance(decoded, dict) and isinstance(decoded.get("sub"), str):
            return decoded["sub"]
    return None


def report(mode: str, steps: list[dict[str, Any]], *, ok: bool) -> dict[str, Any]:
    return {"ok": ok, "mode": mode, "steps": steps}


class CheckError(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


def run_files(
    *,
    list_tools: Callable[[], list[Any]],
    call_tool: Callable[[str, dict[str, Any]], Any],
    put: Callable[[str, bytes], int],
    get: Callable[[str], bytes],
    body: bytes,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    names = tool_names(list_tools())
    needed = ("files_create_upload_url", "files_get_download_link")
    missing = [name for name in needed if name not in names]
    steps.append({"name": "list_tools", "ok": not missing, "missing": missing})
    if missing:
        raise CheckError("list_tools", "required file tools are missing")
    created = payload_of(
        call_tool(
            "files_create_upload_url",
            {"filename": "e2e.bin", "content_type": FILES_UPLOAD_CONTENT_TYPE},
        )
    )
    if not isinstance(created, dict):
        raise CheckError("create_upload_url", "tool result was not an object")
    upload_url = created.get("upload_url")
    file_id = created.get("file_id")
    if not isinstance(upload_url, str) or not isinstance(file_id, str):
        raise CheckError("create_upload_url", "upload_url or file_id missing")
    if not file_id.startswith("eph:"):
        raise CheckError("create_upload_url", "file_id does not start with eph:")
    steps.append(
        {
            "name": "create_upload_url",
            "ok": True,
            "file_id_prefix": "eph:",
            "upload": host_path(upload_url),
        }
    )
    status = put(upload_url, body)
    steps.append({"name": "put", "ok": status in {200, 201, 204}, "http_status": status})
    if status not in {200, 201, 204}:
        raise CheckError("put", f"upload HTTP {status}")
    downloaded = payload_of(call_tool("files_get_download_link", {"file_id": file_id}))
    if not isinstance(downloaded, dict) or not isinstance(downloaded.get("file_url"), str):
        raise CheckError("get_download_link", "file_url missing")
    file_url = downloaded["file_url"]
    steps.append({"name": "get_download_link", "ok": True, "download": host_path(file_url)})
    fetched = get(file_url)
    digest = hashlib.sha256(body).hexdigest()
    same = hashlib.sha256(fetched).hexdigest() == digest
    steps.append({"name": "get", "ok": same, "sha256": digest, "bytes": len(fetched)})
    if not same:
        raise CheckError("get", "downloaded bytes do not match the upload")
    return report("files", steps, ok=True)


def run_hello(
    *,
    namespace: str,
    list_tools: Callable[[], list[Any]],
    call_tool: Callable[[str, dict[str, Any]], Any],
) -> dict[str, Any]:
    names = tool_names(list_tools())
    prefix = f"{namespace}_"
    matched = [name for name in names if name.startswith(prefix)]
    who = f"{namespace}_whoami"
    steps = [{"name": "list_tools", "ok": bool(matched) and who in names, "count": len(matched)}]
    if who not in names:
        raise CheckError("list_tools", f"{who} is not in the tool list")
    payload = payload_of(call_tool(who, {}))
    sub = whoami_sub(payload)
    steps.append({"name": "whoami", "ok": sub == "svc:e2e", "sub": sub})
    if sub != "svc:e2e":
        raise CheckError("whoami", "identity subject was not svc:e2e")
    return report("hello", steps, ok=True)


def run_absent(*, namespace: str, list_tools: Callable[[], list[Any]]) -> dict[str, Any]:
    names = tool_names(list_tools())
    prefix = f"{namespace}_"
    matched = [name for name in names if name.startswith(prefix)]
    steps = [{"name": "list_tools", "ok": not matched, "count": len(matched)}]
    if matched:
        raise CheckError("list_tools", f"tools with prefix {prefix} are still present")
    return report("absent", steps, ok=True)


def _client(url: str, key: str) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {key}"})
    return Client(transport)


def _http(ca: str | None) -> Any:
    import httpx

    return httpx.Client(verify=ca or True, timeout=60.0, follow_redirects=False)


async def _execute(args: argparse.Namespace, url: str, key: str) -> dict[str, Any]:
    async with _client(url, key) as client:
        tools = list(await client.list_tools())
        if args.mode == "absent":
            return run_absent(namespace=args.namespace, list_tools=lambda: tools)
        if args.mode == "hello":
            names = tool_names(tools)
            who = f"{args.namespace}_whoami"
            matched = [name for name in names if name.startswith(f"{args.namespace}_")]
            steps: list[dict[str, Any]] = [
                {"name": "list_tools", "ok": who in names and bool(matched), "count": len(matched)}
            ]
            if who not in names:
                raise CheckError("list_tools", f"{who} is not in the tool list")
            payload = payload_of(await client.call_tool(who, {}))
            sub = whoami_sub(payload)
            steps.append({"name": "whoami", "ok": sub == "svc:e2e", "sub": sub})
            if sub != "svc:e2e":
                raise CheckError("whoami", "identity subject was not svc:e2e")
            return report("hello", steps, ok=True)

        names = tool_names(tools)
        needed = ("files_create_upload_url", "files_get_download_link")
        missing = [name for name in needed if name not in names]
        if missing:
            raise CheckError("list_tools", "required file tools are missing")
        body = os.urandom(MIB)
        created = payload_of(
            await client.call_tool(
                "files_create_upload_url",
                {"filename": "e2e.bin", "content_type": FILES_UPLOAD_CONTENT_TYPE},
            )
        )
        with _http(args.ca) as http:

            def put(target: str, payload: bytes) -> int:
                return http.put(
                    target,
                    content=payload,
                    headers={"Content-Type": FILES_UPLOAD_CONTENT_TYPE},
                ).status_code

            def get(target: str) -> bytes:
                response = http.get(target)
                response.raise_for_status()
                return response.content

            if not isinstance(created, dict):
                raise CheckError("create_upload_url", "tool result was not an object")
            upload_url = created.get("upload_url")
            file_id = created.get("file_id")
            if not isinstance(upload_url, str) or not isinstance(file_id, str):
                raise CheckError("create_upload_url", "upload_url or file_id missing")
            if not file_id.startswith("eph:"):
                raise CheckError("create_upload_url", "file_id does not start with eph:")
            status = put(upload_url, body)
            if status not in {200, 201, 204}:
                raise CheckError("put", f"upload HTTP {status}")
            downloaded = payload_of(
                await client.call_tool("files_get_download_link", {"file_id": file_id})
            )
            if not isinstance(downloaded, dict) or not isinstance(downloaded.get("file_url"), str):
                raise CheckError("get_download_link", "file_url missing")
            file_url = downloaded["file_url"]
            fetched = get(file_url)
            digest = hashlib.sha256(body).hexdigest()
            if hashlib.sha256(fetched).hexdigest() != digest:
                raise CheckError("get", "downloaded bytes do not match the upload")
            steps = [
                {"name": "list_tools", "ok": True, "missing": []},
                {
                    "name": "create_upload_url",
                    "ok": True,
                    "file_id_prefix": "eph:",
                    "upload": host_path(upload_url),
                },
                {"name": "put", "ok": True, "http_status": status},
                {"name": "get_download_link", "ok": True, "download": host_path(file_url)},
                {"name": "get", "ok": True, "sha256": digest, "bytes": len(fetched)},
            ]
            return report("files", steps, ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check a Keystone MCP tool from inside e2e")
    parser.add_argument("--keystone", required=True, help="Keystone base URL, no secret")
    parser.add_argument("--mode", required=True, choices=("files", "hello", "absent"))
    parser.add_argument("--namespace", default="")
    parser.add_argument("--ca", default=None, help="PEM CA for the public HTTPS checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode in {"hello", "absent"} and not args.namespace:
        print("hello and absent need --namespace", file=sys.stderr)
        return 2
    key = os.environ.get("E2E_API_KEY", "")
    if not key:
        document = report(
            args.mode,
            [{"name": "auth", "ok": False, "error": "E2E_API_KEY is not set"}],
            ok=False,
        )
        json.dump(document, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    try:
        document = asyncio.run(_execute(args, mcp_endpoint(args.keystone), key))
    except CheckError as exc:
        document = report(
            args.mode, [{"name": exc.step, "ok": False, "error": exc.message}], ok=False
        )
        json.dump(document, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    except Exception as exc:  # noqa: BLE001 — the message can contain a token URL
        document = report(
            args.mode,
            [{"name": "client", "ok": False, "error": type(exc).__name__}],
            ok=False,
        )
        json.dump(document, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
