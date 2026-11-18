"""Gateway behaviour driven over ASGI with real child processes."""

import asyncio
import json
import os
import unittest

from icedos_mcp_gateway.app import create_app
from icedos_mcp_gateway.config import parse

import support
from support import call, children, config_dict, initialize, make_client, post, running, server_spec, status, tool_text, wait_for


def own_uid(*_args, **_kwargs):
    return frozenset({os.getuid()})


def build(
    servers=None,
    *,
    idle_timeout=600.0,
    reap_interval=0.0,
    request_timeout=None,
    session_ttl=None,
):
    config = parse(config_dict(servers or {"fake": server_spec("per-agent")}, idle_timeout=idle_timeout))
    return create_app(
        config,
        peer_lookup=own_uid,
        reap_interval=reap_interval,
        request_timeout=request_timeout,
        session_ttl=session_ttl,
    )


class PerAgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = build()
        self._life = running(self.app)
        await self._life.__aenter__()
        self.client = make_client(self.app)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self._life.__aexit__(None, None, None)

    async def test_sessions_get_their_own_child(self):
        first, _ = await initialize(self.client)
        second, _ = await initialize(self.client)
        self.assertNotEqual(first, second)
        one = json.loads(tool_text(await call(self.client, first, "pid")))
        two = json.loads(tool_text(await call(self.client, second, "pid")))
        self.assertNotEqual(one["pid"], two["pid"])
        self.assertEqual(one["cwd"], os.path.expanduser("~"))
        listed = await status(self.client)
        self.assertEqual(len(children(listed)), 2)

    async def test_env_is_passed_to_the_child(self):
        app = build({"fake": server_spec("per-agent", env={"FAKE_TAG": "hello"})})
        async with running(app), make_client(app) as client:
            session, _ = await initialize(client)
            payload = json.loads(tool_text(await call(client, session, "pid")))
            self.assertEqual(payload["env"], {"FAKE_TAG": "hello"})

    async def test_initialize_result_is_passed_through(self):
        session, result = await initialize(self.client)
        self.assertEqual(result["serverInfo"]["name"], "fake-mcp")
        self.assertEqual(result["instructions"], "fake instructions")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])

    async def test_delete_kills_the_child_and_invalidates_the_session(self):
        session, _ = await initialize(self.client)
        pid = json.loads(tool_text(await call(self.client, session, "pid")))["pid"]
        response = await self.client.delete("/fake", headers={"mcp-session-id": session})
        self.assertEqual(response.status_code, 204)
        await wait_for(lambda: _no_children(self.client))
        await wait_for(lambda: not support.pid_alive(pid))
        response = await self.client.post(
            "/fake",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            headers={"mcp-session-id": session, "accept": "application/json"},
        )
        self.assertEqual(response.status_code, 404)

    async def test_idle_reap_restarts_the_child_and_keeps_the_session(self):
        app = build(idle_timeout=0.2, reap_interval=0.05)
        async with running(app), make_client(app) as client:
            session, _ = await initialize(client)
            before = json.loads(tool_text(await call(client, session, "pid")))
            self.assertEqual((before["inits"], before["initialized"]), (1, 1))
            await wait_for(lambda: _no_children(client))
            await wait_for(lambda: not support.pid_alive(before["pid"]))
            # The session still works: the child is respawned and replayed.
            after = json.loads(tool_text(await call(client, session, "pid")))
            self.assertNotEqual(before["pid"], after["pid"])
            self.assertEqual((after["inits"], after["initialized"]), (1, 1))
            self.assertEqual(len(children(await status(client))), 1)

    async def test_sessions_expire_after_the_session_ttl(self):
        app = build(idle_timeout=0.2, reap_interval=0.05, session_ttl=0.3)
        async with running(app), make_client(app) as client:
            session, _ = await initialize(client)
            pid = json.loads(tool_text(await call(client, session, "pid")))["pid"]
            await wait_for(lambda: _no_children(client))
            # No request may touch the session while its ttl runs out.
            await asyncio.sleep(0.6)
            response = await post(
                client, "fake", {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, session
            )
            self.assertEqual(response.status_code, 404)
            await wait_for(lambda: not support.pid_alive(pid))

    async def test_crash_restarts_the_child_and_keeps_the_session(self):
        session, _ = await initialize(self.client)
        before = json.loads(tool_text(await call(self.client, session, "pid")))
        self.assertEqual((before["inits"], before["initialized"]), (1, 1))
        # The request in flight at the crash still gets the error reply.
        reply = await call(self.client, session, "crash")
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertIn("exited", reply["error"]["message"])
        await wait_for(lambda: not support.pid_alive(before["pid"]))
        # The session survives the crash: the child is respawned and replayed.
        after = json.loads(tool_text(await call(self.client, session, "pid")))
        self.assertNotEqual(before["pid"], after["pid"])
        self.assertEqual((after["inits"], after["initialized"]), (1, 1))
        self.assertEqual(len(children(await status(self.client))), 1)


class GlobalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = build({"fake": server_spec("global")})
        self._life = running(self.app)
        await self._life.__aenter__()
        self.client = make_client(self.app)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self._life.__aexit__(None, None, None)

    async def test_sessions_share_one_child(self):
        first, _ = await initialize(self.client)
        second, _ = await initialize(self.client)
        self.assertNotEqual(first, second)
        one = json.loads(tool_text(await call(self.client, first, "pid")))
        two = json.loads(tool_text(await call(self.client, second, "pid")))
        self.assertEqual(one["pid"], two["pid"])
        self.assertEqual(len(children(await status(self.client))), 1)

    async def test_second_initialize_is_answered_from_the_cache(self):
        _, first = await initialize(self.client)
        _, second = await initialize(self.client)
        self.assertEqual(first, second)

    async def test_second_initialize_echoes_the_requested_version(self):
        _, first = await initialize(self.client)
        _, older = await initialize(self.client, protocol_version="2024-11-05")
        self.assertEqual(first["protocolVersion"], "2025-06-18")
        self.assertEqual(older["protocolVersion"], "2024-11-05")
        self.assertEqual(older["serverInfo"], first["serverInfo"])

    async def test_idle_reap_restarts_the_child_and_keeps_the_session(self):
        app = build({"fake": server_spec("global")}, idle_timeout=0.2, reap_interval=0.05)
        async with running(app), make_client(app) as client:
            session, first = await initialize(client)
            before = json.loads(tool_text(await call(client, session, "pid")))
            self.assertEqual((before["inits"], before["initialized"]), (1, 1))
            await wait_for(lambda: _no_children(client))
            await wait_for(lambda: not support.pid_alive(before["pid"]))
            # The same session keeps working on a fresh child, and GET / attributes
            # the lazily restarted child to the session that revived it.
            after = json.loads(tool_text(await call(client, session, "pid")))
            self.assertNotEqual(before["pid"], after["pid"])
            self.assertEqual((after["inits"], after["initialized"]), (1, 1))
            listed = children(await status(client))
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["sessionId"], session)
            # A later initialize is still answered from the cache.
            fresh, second = await initialize(client)
            self.assertNotEqual(fresh, session)
            self.assertEqual(second, first)

    async def test_global_sessions_expire_after_the_session_ttl(self):
        app = build(
            {"fake": server_spec("global")},
            idle_timeout=0.2,
            reap_interval=0.05,
            session_ttl=0.3,
        )
        async with running(app), make_client(app) as client:
            busy, _ = await initialize(client)
            idle, _ = await initialize(client)
            pid = json.loads(tool_text(await call(client, busy, "pid")))["pid"]
            for _ in range(6):  # keep one session (and the child) alive past the ttl
                await asyncio.sleep(0.1)
                await call(client, busy, "pid")
            stale = await post(
                client, "fake", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, idle
            )
            self.assertEqual(stale.status_code, 404)
            still = json.loads(tool_text(await call(client, busy, "pid")))["pid"]
            self.assertEqual(still, pid)

    async def test_request_timeout_releases_the_child(self):
        app = build(
            {"fake": server_spec("global")},
            idle_timeout=0.4,
            reap_interval=0.05,
            request_timeout=0.2,
        )
        async with running(app), make_client(app) as client:
            session, _ = await initialize(client)
            pid = json.loads(tool_text(await call(client, session, "pid")))["pid"]
            reply = await call(client, session, "hang", request_id=7)
            self.assertEqual(reply["error"]["code"], -32603)
            self.assertIn("did not answer", reply["error"]["message"])
            # Without the timeout the child stays inflight and is never reaped.
            await wait_for(lambda: _no_children(client))
            await wait_for(lambda: not support.pid_alive(pid))

    async def test_crash_is_reported_then_restarted(self):
        session, _ = await initialize(self.client)
        before = json.loads(tool_text(await call(self.client, session, "pid")))["pid"]
        reply = await call(self.client, session, "crash")
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertIn("exited", reply["error"]["message"])
        after = json.loads(tool_text(await call(self.client, session, "pid")))["pid"]
        self.assertNotEqual(before, after)

    async def test_delete_keeps_the_shared_child(self):
        session, _ = await initialize(self.client)
        other, _ = await initialize(self.client)
        pid = json.loads(tool_text(await call(self.client, session, "pid")))["pid"]
        response = await self.client.delete("/fake", headers={"mcp-session-id": session})
        self.assertEqual(response.status_code, 204)
        still = json.loads(tool_text(await call(self.client, other, "pid")))["pid"]
        self.assertEqual(pid, still)


class ProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = build()
        self._life = running(self.app)
        await self._life.__aenter__()
        self.client = make_client(self.app)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self._life.__aexit__(None, None, None)

    async def test_status_lists_servers_without_children(self):
        payload = await status(self.client)
        self.assertEqual(payload, {"servers": {"fake": {"mode": "per-agent", "children": []}}})
        session, _ = await initialize(self.client)
        payload = await status(self.client)
        listed = children(payload)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["sessionId"], session)
        self.assertGreaterEqual(listed[0]["idleSec"], 0)

    async def test_unknown_server_is_404(self):
        response = await self.client.post(
            "/nope", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"accept": "application/json"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.client.get("/nope")).status_code, 404)
        self.assertEqual(
            (await self.client.delete("/nope", headers={"mcp-session-id": "x"})).status_code, 404
        )

    async def test_trailing_slash_is_accepted(self):
        response = await self.client.post(
            "/fake/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
            headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        session = response.headers["mcp-session-id"]
        await self.client.delete("/fake", headers={"mcp-session-id": session})

    async def test_bad_content_type(self):
        response = await self.client.post(
            "/fake", content=b"{}", headers={"content-type": "text/plain", "accept": "application/json"}
        )
        self.assertEqual(response.status_code, 415)

    async def test_parse_error(self):
        response = await self.client.post(
            "/fake",
            content=b"{oops",
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32700)

    async def test_batch_is_rejected(self):
        response = await self.client.post(
            "/fake",
            json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
            headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32600)

    async def test_missing_session_header(self):
        response = await self.client.post(
            "/fake", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json"}
        )
        self.assertEqual(response.status_code, 400)

    async def test_unknown_session(self):
        response = await self.client.post(
            "/fake",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"mcp-session-id": "deadbeef", "accept": "application/json"},
        )
        self.assertEqual(response.status_code, 404)

    async def test_notification_gets_202(self):
        session, _ = await initialize(self.client)
        response = await post(
            self.client, "fake", {"jsonrpc": "2.0", "method": "notifications/initialized"}, session
        )
        self.assertEqual(response.status_code, 202)

    async def test_sse_only_client_gets_an_event(self):
        response = await self.client.post(
            "/fake",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
            headers={"accept": "text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: message", response.text)
        session = response.headers["mcp-session-id"]
        await self.client.delete("/fake", headers={"mcp-session-id": session})

    async def test_get_stream_unknown_session(self):
        response = await self.client.get(
            "/fake", headers={"mcp-session-id": "nope", "accept": "text/event-stream"}
        )
        self.assertEqual(response.status_code, 404)
        response = await self.client.get("/fake", headers={"accept": "text/event-stream"})
        self.assertEqual(response.status_code, 400)


async def _no_children(client):
    return not children(await status(client))


if __name__ == "__main__":
    unittest.main()
