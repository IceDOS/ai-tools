"""Shared helpers for the gateway tests."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

HERE = Path(__file__).resolve().parent
FAKE_SERVER = str(HERE / "fake_server.py")
PYTHON = sys.executable


def server_spec(mode: str = "per-agent", name: str = "fake", env: dict | None = None) -> dict:
    return {
        "mode": mode,
        "command": PYTHON,
        "args": [FAKE_SERVER],
        "env": env or {},
    }


def config_dict(
    servers: dict[str, dict] | None = None,
    *,
    idle_timeout: float = 600.0,
    host: str = "127.0.0.1",
    port: int = 7800,
) -> dict:
    return {
        "host": host,
        "port": port,
        "idleTimeoutSec": idle_timeout,
        "servers": servers if servers is not None else {"fake": server_spec()},
    }


@contextlib.asynccontextmanager
async def running(app) -> AsyncIterator[None]:
    """Run the application lifespan (reaper plus child shutdown)."""
    async with app.router.lifespan_context(app):
        yield


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )


HEADERS = {"accept": "application/json, text/event-stream"}


async def post(client: httpx.AsyncClient, name: str, message: dict, session: str | None = None) -> httpx.Response:
    headers = dict(HEADERS)
    if session is not None:
        headers["mcp-session-id"] = session
    return await client.post(f"/{name}", json=message, headers=headers)


async def initialize(
    client: httpx.AsyncClient, name: str = "fake", protocol_version: str = "2025-06-18"
) -> tuple[str, dict]:
    response = await post(
        client,
        name,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "0"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session = response.headers["mcp-session-id"]
    ack = await post(client, name, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    assert ack.status_code == 202, ack.text
    return session, response.json()["result"]


async def call(
    client: httpx.AsyncClient,
    session: str,
    tool: str,
    arguments: dict | None = None,
    *,
    name: str = "fake",
    request_id: int = 2,
) -> dict:
    response = await post(
        client,
        name,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
        session,
    )
    assert response.status_code == 200, response.text
    return response.json()


def tool_text(reply: dict) -> str:
    return reply["result"]["content"][0]["text"]


async def status(client: httpx.AsyncClient) -> dict:
    response = await client.get("/")
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> Any:
    """Poll an async predicate until it returns a truthy value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


def children(payload: dict, name: str = "fake") -> list[dict]:
    return payload["servers"][name]["children"]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
