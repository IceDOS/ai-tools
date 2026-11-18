#!/usr/bin/env python3
"""A tiny stdio MCP server used by the gateway tests.

Speaks newline-delimited JSON-RPC on stdin/stdout and exposes one tool per
behaviour the gateway needs to exercise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

SERVER_INFO = {"name": "fake-mcp", "version": "0.0.1"}
INSTRUCTIONS = "fake instructions"

CAPABILITIES = {
    "tools": {"listChanged": True},
    "resources": {"listChanged": True, "subscribe": False},
    "prompts": {"listChanged": True},
    "logging": {},
    "completions": {},
}

TOOLS = [
    {"name": "echo", "description": "echo text", "inputSchema": {"type": "object"}},
    {"name": "pid", "description": "report pid, cwd and env", "inputSchema": {"type": "object"}},
    {"name": "sleep", "description": "sleep", "inputSchema": {"type": "object"}},
    {"name": "hang", "description": "never answer", "inputSchema": {"type": "object"}},
    {"name": "notify", "description": "send notifications", "inputSchema": {"type": "object"}},
    {"name": "ask_client", "description": "sample from the client", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "exit abruptly", "inputSchema": {"type": "object"}},
    {"name": "stderr", "description": "write to stderr", "inputSchema": {"type": "object"}},
]

_pending: dict[str, asyncio.Future] = {}
_server_request = 0


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def send_to_client(method: str, params: dict) -> asyncio.Future:
    """Issue a server-to-client request and wait for the client's reply."""
    global _server_request
    _server_request += 1
    request_id = f"srv-{_server_request}"
    future = asyncio.get_running_loop().create_future()
    _pending[request_id] = future
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return future


_inits = 0
_initialized = 0


async def call_tool(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "echo":
        return text_result(str(arguments.get("text", "")))
    if name == "pid":
        payload = {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "env": {key: value for key, value in os.environ.items() if key.startswith("FAKE_")},
            "inits": _inits,
            "initialized": _initialized,
        }
        return text_result(json.dumps(payload))
    if name == "sleep":
        await asyncio.sleep(float(arguments.get("seconds", 0.1)))
        return text_result("slept")
    if name == "hang":
        print("hang started", file=sys.stderr, flush=True)
        await asyncio.Event().wait()  # never replies; the gateway must time out
        return text_result("unreachable")
    if name == "notify":
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "fake log line"},
            }
        )
        return text_result("notified")
    if name == "ask_client":
        try:
            reply = await asyncio.wait_for(
                send_to_client(
                    "sampling/createMessage",
                    {
                        "messages": [
                            {"role": "user", "content": {"type": "text", "text": "hi"}}
                        ],
                        "maxTokens": 16,
                    },
                ),
                timeout=10,
            )
        except asyncio.TimeoutError:
            return text_result("timeout")
        return text_result(json.dumps(reply))
    if name == "crash":
        sys.stdout.flush()
        os._exit(7)
    if name == "stderr":
        sys.stderr.write("fake stderr line\n")
        sys.stderr.flush()
        return text_result("wrote stderr")
    return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name!r}"}]}


async def handle_request(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        global _inits
        _inits += 1
        requested = params.get("protocolVersion")
        result = {
            "protocolVersion": requested or "2025-06-18",
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = await call_tool(params)
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "completion/complete":
        result = {"completion": {"values": [], "total": 0, "hasMore": False}}
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        )
        return
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


async def handle_message(message: dict) -> None:
    if "method" not in message:
        future = _pending.pop(str(message.get("id")), None)
        if future is not None and not future.done():
            future.set_result(message)
        return
    if "id" not in message:
        if message.get("method") == "notifications/initialized":
            global _initialized
            _initialized += 1
        return  # notifications need no reply
    if message.get("method") == "crash":
        os._exit(7)
    await handle_request(message)


def read_line() -> bytes:
    return sys.stdin.buffer.readline()


async def main() -> int:
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task] = set()
    while True:
        line = await loop.run_in_executor(None, read_line)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        task = asyncio.create_task(handle_message(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    for task in list(tasks):
        task.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
