"""Minimal JSON-RPC 2.0 inspection and message builders.

The gateway never models MCP payloads; it only needs to tell requests,
notifications and responses apart and to build error replies.
"""

from __future__ import annotations

from typing import Any

from mcp.types import (  # noqa: F401  (re-exported for the rest of the package)
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)

JSONRPC_VERSION = "2.0"

REQUEST = "request"
NOTIFICATION = "notification"
RESPONSE = "response"


def is_valid(message: Any) -> bool:
    """True for a JSON-RPC 2.0 object."""
    return isinstance(message, dict) and message.get("jsonrpc") == JSONRPC_VERSION


def is_request(message: Any) -> bool:
    return is_valid(message) and "method" in message and "id" in message


def is_notification(message: Any) -> bool:
    return is_valid(message) and "method" in message and "id" not in message


def is_response(message: Any) -> bool:
    return (
        is_valid(message)
        and "method" not in message
        and "id" in message
        and ("result" in message or "error" in message)
    )


def classify(message: Any) -> str | None:
    """Return 'request', 'notification', 'response' or None."""
    if is_request(message):
        return REQUEST
    if is_notification(message):
        return NOTIFICATION
    if is_response(message):
        return RESPONSE
    return None


def error_response(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC error reply."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def response(request_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success reply."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def request(request_id: Any, method: str, params: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC request."""
    message: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
    }
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC notification."""
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message
