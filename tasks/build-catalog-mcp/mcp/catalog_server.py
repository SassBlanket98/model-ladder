"""A small MCP server over stdio: the product catalogue a task's agent has to query.

Standard library only. Speaks JSON-RPC 2.0, one JSON message per line, and implements the
parts of the Model Context Protocol a tool-only server needs: initialize, ping, tools/list
and tools/call. Every tool call is appended to the file named by MCP_CALL_LOG, so a task's
check can see which tools the agent really used.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CATALOG = json.loads((Path(__file__).parent / "catalog.json").read_text(encoding="utf-8"))

# Catalogue tools read local data. These hints let clients classify their risk;
# optional call logging does not change the catalogue.
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_skus",
        "description": "List every product currently sold: SKU and name. Prices are not included.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "get_price",
        "description": "Current unit price of one SKU, in whole cents.",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "SKU, e.g. WID-1"}},
            "required": ["sku"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "get_discount_policy",
        "description": "The bulk discount policy: quantity tiers, how a line is discounted and "
        "how it is rounded.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
]


class ToolError(Exception):
    """A failure the model should see and can act on (returned with isError: true)."""


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    allowed = {"sku"} if name == "get_price" else set()
    if set(args) - allowed:
        raise ToolError(f"unexpected arguments for {name}")
    if name == "list_skus":
        return {"skus": [{"sku": s, "name": p["name"]} for s, p in CATALOG["products"].items()]}
    if name == "get_price":
        sku = args.get("sku")
        if not isinstance(sku, str):
            raise ToolError("get_price needs a string argument 'sku'")
        product = CATALOG["products"].get(sku)
        if product is None:
            raise ToolError(f"unknown SKU {sku!r}; call list_skus for the valid ones")
        return {"sku": sku, "unit_price_cents": product["unit_price_cents"], "currency": "USD"}
    if name == "get_discount_policy":
        return CATALOG["discount_policy"]
    raise KeyError(name)


def log_call(name: str, args: dict[str, Any], ok: bool) -> None:
    path = os.environ.get("MCP_CALL_LOG")
    if not path:
        return
    entry = {"at": datetime.now(UTC).isoformat(), "tool": name, "arguments": args, "ok": ok}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Result for one request. Raises LookupError for an unknown method or tool."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "catalog", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        if not any(t["name"] == name for t in TOOLS):
            raise LookupError(f"unknown tool {name!r}")
        try:
            data = call_tool(name, args)
        except ToolError as exc:
            log_call(name, args, ok=False)
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        log_call(name, args, ok=True)
        return {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
            "isError": False,
        }
    raise LookupError(f"method not found: {method}")


def reply(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad JSON"}})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue  # a response or something else we never asked for
        if "id" not in msg:
            continue  # notification, e.g. notifications/initialized: no reply
        try:
            result = handle(msg["method"], msg.get("params", {}))
        except ValueError as exc:
            reply({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": str(exc)}})
            continue
        except LookupError as exc:
            code = -32602 if msg["method"] == "tools/call" else -32601
            reply({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": str(exc)}})
            continue
        reply({"jsonrpc": "2.0", "id": msg["id"], "result": result})


if __name__ == "__main__":
    main()
