"""Check the installed app-server's public protocol without starting a turn."""

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def supports_turn_steer(cli: Path) -> bool:
    try:
        with TemporaryDirectory(prefix="milo-protocol-") as directory:
            root = Path(directory)
            schema = root / "schema"
            subprocess.run(
                [str(cli), "app-server", "generate-json-schema", "--out", str(schema)],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "CODEX_HOME": str(root)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=3,
            )
            with (schema / "ClientRequest.json").open("rb") as stream:
                request = _read_schema(stream, 4 * 1024 * 1024)
            with (schema / "v2" / "TurnSteerResponse.json").open("rb") as stream:
                response = _read_schema(stream, 8192)
            return _matches(request, response)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return False


def _read_schema(stream, limit):
    content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Protocol schema exceeds the probe limit")
    return json.loads(content)


def _matches(request, response):
    params_name = "TurnSteerParams"
    methods = (item.get("properties", {}) for item in request.get("oneOf", []))
    params = request.get("definitions", {}).get(params_name, {})
    fields = {"threadId", "expectedTurnId", "clientUserMessageId", "input"}
    properties = params.get("properties", {})
    return (
        any(
            entry.get("method", {}).get("enum") == ["turn/steer"]
            and entry.get("params", {}).get("$ref") == "#/definitions/" + params_name
            for entry in methods
        )
        and fields <= properties.keys()
        and properties["threadId"].get("type") == "string"
        and properties["expectedTurnId"].get("type") == "string"
        and properties["input"].get("type") == "array"
        and properties["clientUserMessageId"].get("type") in ("string", ["string", "null"])
        and {"threadId", "expectedTurnId", "input"} <= set(params.get("required", [])) <= fields
        and response.get("properties", {}).get("turnId", {}).get("type") == "string"
        and "turnId" in response.get("required", [])
    )
