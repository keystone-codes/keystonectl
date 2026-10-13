"""Load the service-package schema and check that it is a valid Draft 2020-12 document."""

import json
from pathlib import Path

from jsonschema.validators import Draft202012Validator

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "keystonectl"
    / "schemas"
    / "service-package-v1.json"
)


def test_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "https://keystone.codes/schemas/service-package-v1.json"
    Draft202012Validator.check_schema(schema)

    assert schema["additionalProperties"] is False
    mcp = schema["$defs"]["mcp"]["properties"]
    assert mcp["transport"]["default"] == "http"
    assert mcp["path"]["default"] == "/mcp"
    assert mcp["healthz"]["default"] == "/healthz"
    assert schema["$defs"]["container"]["properties"]["init"]["default"] is False
    health = schema["$defs"]["healthcheck"]["properties"]
    assert health["interval"]["default"] == "10s"
    assert health["timeout"]["default"] == "5s"
    assert health["start_period"]["default"] == "20s"
    assert health["retries"]["default"] == 5
    assert schema["$defs"]["envVar"]["properties"]["required"]["default"] is False
