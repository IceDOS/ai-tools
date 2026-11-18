"""End-to-end test: real uvicorn server and the official MCP client."""

import asyncio
import io
import json
import os
import socket
import struct
import unittest

import httpx
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import support
from icedos_mcp_gateway.app import create_app
from icedos_mcp_gateway.config import parse
from support import children, config_dict, server_spec, wait_for


async def _started(server) -> bool:
    return server.started


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = parse(config_dict({"fake": server_spec("per-agent")}))
        self.app = create_app(self.config, reap_interval=0.5)
        server_config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            access_log=False,
            lifespan="on",
        )
        self.server = uvicorn.Server(server_config)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.base}/fake"
        self.main = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        await wait_for(lambda: _started(self.server))

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.main, 20)
        self.sock.close()

    async def status(self) -> dict:
        async with httpx.AsyncClient(base_url=self.base) as client:
            response = await client.get("/")
            self.assertEqual(response.status_code, 200)
            return response.json()

    async def test_client_round_trip(self):
        pids = []
        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                self.assertEqual(init.serverInfo.name, "fake-mcp")
                self.assertEqual(init.instructions, "fake instructions")
                self.assertIn("tools", init.capabilities.model_fields_set)

                tools = await session.list_tools()
                self.assertIn("echo", {tool.name for tool in tools.tools})

                result = await session.call_tool("echo", {"text": "hello"})
                self.assertEqual(result.content[0].text, "hello")

                result = await session.call_tool("pid", {})
                payload = json.loads(result.content[0].text)
                pids.append(payload["pid"])
                self.assertNotEqual(payload["pid"], os.getpid())

                listed = children(await self.status())
                self.assertEqual([entry["pid"] for entry in listed], [payload["pid"]])

        await wait_for(lambda: self._no_children())
        self.assertFalse(support.pid_alive(pids[0]))

    async def test_notifications_reach_the_client(self):
        received = []

        async def handler(message):
            if isinstance(message, types.ServerNotification):
                received.append(message.root)

        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write, message_handler=handler) as session:
                await session.initialize()
                result = await session.call_tool("notify", {})
                self.assertEqual(result.content[0].text, "notified")
                await wait_for(
                    lambda: any(isinstance(item, types.ToolListChangedNotification) for item in received),
                    timeout=15,
                )

    async def test_second_get_stream_is_rejected(self):
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
        timeout = httpx.Timeout(30.0, read=None)
        async with httpx.AsyncClient(base_url=self.base, timeout=timeout) as client:
            response = await client.post(
                "/fake", json=init, headers={"accept": "application/json, text/event-stream"}
            )
            self.assertEqual(response.status_code, 200)
            session_id = response.headers["mcp-session-id"]
            headers = {"mcp-session-id": session_id, "accept": "text/event-stream"}
            async with client.stream("GET", "/fake", headers=headers) as first:
                self.assertEqual(first.status_code, 200)
                second = await client.get("/fake", headers=headers)
                self.assertEqual(second.status_code, 409)
            response = await client.delete("/fake", headers={"mcp-session-id": session_id})
            self.assertEqual(response.status_code, 204)

    async def test_foreign_uid_is_rejected(self):
        # The real lookup finds our own uid, so the same request with a lookup
        # that reports another uid must be refused.
        app = create_app(self.config, peer_lookup=lambda *a, **k: frozenset({os.getuid() + 1}))
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, lifespan="on")
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await wait_for(lambda: _started(server))
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.get("/")
                self.assertEqual(response.status_code, 403)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 20)
            sock.close()

    async def test_client_disconnect_releases_the_child(self):
        # Hanging up mid-request must not leave the child inflight forever:
        # otherwise the idle reaper could never free it.
        sink = io.StringIO()
        config = parse(config_dict({"fake": server_spec("per-agent")}, idle_timeout=0.4))
        app = create_app(
            config, reap_interval=0.05, stderr_sink=sink, request_timeout=60.0
        )
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, lifespan="on")
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await wait_for(lambda: _started(server))
            async with httpx.AsyncClient(base_url=base) as client:
                response = await client.post(
                    "/fake",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                    },
                    headers={"accept": "application/json"},
                )
                self.assertEqual(response.status_code, 200)
                session_id = response.headers["mcp-session-id"]
                await client.post(
                    "/fake",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"accept": "application/json", "mcp-session-id": session_id},
                )
                pid = children((await client.get("/")).json())[0]["pid"]

            raw = socket.create_connection(("127.0.0.1", port))
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "hang", "arguments": {}},
                }
            ).encode()
            raw.sendall(
                b"POST /fake HTTP/1.1\r\nHost: gateway\r\nContent-Type: application/json\r\n"
                + b"Accept: application/json\r\nMcp-Session-Id: "
                + session_id.encode()
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await wait_for(lambda: "hang started" in sink.getvalue(), timeout=15)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            raw.close()

            async def reaped() -> bool:
                async with httpx.AsyncClient(base_url=base) as client:
                    return not children((await client.get("/")).json())

            await wait_for(reaped, timeout=20)
            await wait_for(lambda: not support.pid_alive(pid), timeout=20)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 20)
            sock.close()

    async def _no_children(self) -> bool:
        return not children(await self.status())


if __name__ == "__main__":
    unittest.main()
