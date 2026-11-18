"""ServerRuntime routing of child messages (no real child process)."""

import unittest

from icedos_mcp_gateway.child import Pending
from icedos_mcp_gateway.config import ServerSpec
from icedos_mcp_gateway.jsonrpc import METHOD_NOT_FOUND
from icedos_mcp_gateway.runtime import Outbox, ServerRuntime, new_session


class FakeChild:
    """Stands in for a ChildProcess; records what the runtime posts to it."""

    def __init__(self):
        self.posted = []
        self.pending = {}
        self.inflight = 0
        self.pid = 4321
        self.idle = 0.0
        self.alive = True
        self.closing = False
        self._seq = 0

    def post(self, message):
        self.posted.append(message)

    def next_request_id(self):
        self._seq += 1
        return f"gw-{self._seq}"

    def touch(self):
        pass

    def idle_seconds(self):
        return self.idle

    async def stop(self):
        self.alive = False

    def cleanup(self):
        pass


def runtime_for(mode: str) -> ServerRuntime:
    return ServerRuntime(ServerSpec(name="fake", mode=mode, command="/bin/true"))


class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_close(self):
        outbox = Outbox()
        outbox.put({"n": 1})
        outbox.put({"n": 2})
        self.assertEqual(await outbox.get(), {"n": 1})
        self.assertEqual(await outbox.get(), {"n": 2})
        outbox.close()
        self.assertIsNone(await outbox.get())
        outbox.put({"n": 3})
        self.assertIsNone(await outbox.get())


class ReapTest(unittest.IsolatedAsyncioTestCase):
    def aged(self, runtime, *, inflight=0, idle=10.0):
        session = new_session("fake", runtime.mode)
        child = FakeChild()
        child.idle = idle
        session.child = child
        session.init = {"protocolVersion": "2025-06-18"}
        session.last_used -= 10.0
        session.inflight = inflight
        runtime.sessions = {session.id: session}
        return session, child

    async def test_reaping_a_per_agent_child_keeps_the_session(self):
        runtime = runtime_for("per-agent")
        session, child = self.aged(runtime)
        await runtime.reap(idle_timeout=1.0)
        self.assertIsNone(session.child)
        self.assertFalse(child.alive)
        self.assertFalse(session.closed)
        self.assertIs(runtime.sessions[session.id], session)
        self.assertEqual(session.init, {"protocolVersion": "2025-06-18"})

    async def test_busy_sessions_are_not_reaped(self):
        runtime = runtime_for("per-agent")
        session, child = self.aged(runtime, inflight=1)
        await runtime.reap(idle_timeout=1.0)
        self.assertIs(session.child, child)
        self.assertTrue(child.alive)
        self.assertIn(session.id, runtime.sessions)

    async def test_sessions_expire_after_the_ttl(self):
        runtime = runtime_for("per-agent")
        session, child = self.aged(runtime)
        await runtime.reap(idle_timeout=1.0, session_ttl=5.0)
        self.assertTrue(session.closed)
        self.assertNotIn(session.id, runtime.sessions)
        self.assertFalse(child.alive)

    async def test_reaping_the_global_child_keeps_its_initialize_cache(self):
        runtime = runtime_for("global")
        child = FakeChild()
        child.idle = 10.0
        runtime.global_child = child
        runtime.global_owner = "abc"
        runtime.global_init = {"protocolVersion": "2025-06-18"}
        session = new_session("fake", "global")
        runtime.sessions = {session.id: session}
        await runtime.reap(idle_timeout=1.0)
        self.assertIsNone(runtime.global_child)
        self.assertIsNone(runtime.global_owner)
        self.assertEqual(runtime.global_init, {"protocolVersion": "2025-06-18"})
        self.assertIn(session.id, runtime.sessions)


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_notification_reaches_every_global_session(self):
        runtime = runtime_for("global")
        first = new_session("fake", "global")
        second = new_session("fake", "global")
        runtime.sessions = {first.id: first, second.id: second}
        await runtime._handle_child_message(
            FakeChild(), {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        )
        for session in (first, second):
            self.assertEqual(await session.outbox.get(), {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    async def test_notification_reaches_the_per_agent_owner_only(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        owner = new_session("fake", "per-agent")
        other = new_session("fake", "per-agent")
        owner.child = child
        runtime.sessions = {owner.id: owner, other.id: other}
        await runtime._handle_child_message(child, {"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
        self.assertIsNotNone(await owner.outbox.get())
        self.assertFalse(other.outbox._items)

    async def test_server_request_is_forwarded_per_agent(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        session = new_session("fake", "per-agent")
        session.child = child
        runtime.sessions = {session.id: session}
        request = {"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}}
        await runtime._handle_child_message(child, request)
        self.assertEqual(await session.outbox.get(), request)
        self.assertEqual(child.posted, [])

    async def test_server_request_is_refused_in_global_mode(self):
        runtime = runtime_for("global")
        child = FakeChild()
        session = new_session("fake", "global")
        runtime.sessions = {session.id: session}
        await runtime._handle_child_message(
            child, {"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}}
        )
        self.assertEqual(child.posted[-1]["id"], "srv-1")
        self.assertEqual(child.posted[-1]["error"]["code"], METHOD_NOT_FOUND)

    async def test_response_restores_the_client_id(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        session = new_session("fake", "per-agent")
        pending = Pending(session=session.id, client_id=17)
        child.pending["gw-4"] = pending
        await runtime._handle_child_message(child, {"jsonrpc": "2.0", "id": "gw-4", "result": {"value": 1}})
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.response, {"jsonrpc": "2.0", "id": 17, "result": {"value": 1}})

    async def test_cancellation_is_remapped(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        session = new_session("fake", "per-agent")
        session.child = child
        runtime.sessions = {session.id: session}
        child.pending["gw-4"] = Pending(session=session.id, client_id=17)
        await runtime.handle_notification(
            session,
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 17}},
        )
        self.assertEqual(child.posted[-1]["params"]["requestId"], "gw-4")

    async def test_global_initialized_is_swallowed(self):
        runtime = runtime_for("global")
        child = FakeChild()
        session = new_session("fake", "global")
        runtime.global_child = child
        runtime.sessions = {session.id: session}
        await runtime.handle_notification(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(child.posted, [])

    async def test_per_agent_initialized_is_forwarded(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        session = new_session("fake", "per-agent")
        session.child = child
        runtime.sessions = {session.id: session}
        await runtime.handle_notification(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(child.posted[-1]["method"], "notifications/initialized")

    async def test_global_ignores_client_responses(self):
        runtime = runtime_for("global")
        child = FakeChild()
        session = new_session("fake", "global")
        runtime.global_child = child
        await runtime.handle_client_response(session, {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(child.posted, [])

    async def test_child_exit_fails_pending_requests_and_keeps_the_session(self):
        runtime = runtime_for("per-agent")
        child = FakeChild()
        session = new_session("fake", "per-agent")
        session.child = child
        session.init = {"protocolVersion": "2025-06-18"}
        runtime.sessions = {session.id: session}
        pending = Pending(session=session.id, client_id=5)
        child.pending["gw-9"] = pending
        await runtime._handle_child_exit(child, 7)
        self.assertTrue(pending.event.is_set())
        self.assertEqual(pending.response["error"]["code"], -32603)
        self.assertIn("exited", pending.response["error"]["message"])
        # A crash is transparent: the session survives without its child so the
        # next request respawns and replays the cached handshake.
        self.assertIn(session.id, runtime.sessions)
        self.assertFalse(session.closed)
        self.assertIsNone(session.child)
        self.assertEqual(session.init, {"protocolVersion": "2025-06-18"})

    async def test_child_exit_keeps_global_initialize_cache(self):
        runtime = runtime_for("global")
        child = FakeChild()
        runtime.global_child = child
        runtime.global_init = {"protocolVersion": "2025-06-18"}
        runtime.global_init_result = {"protocolVersion": "2025-06-18"}
        await runtime._handle_child_exit(child, 1)
        self.assertIsNone(runtime.global_child)
        self.assertIsNotNone(runtime.global_init)

    async def test_drop_session_does_not_touch_the_global_child(self):
        runtime = runtime_for("global")
        child = FakeChild()
        runtime.global_child = child
        session = new_session("fake", "global")
        runtime.sessions = {session.id: session}
        await runtime.drop_session(session)
        self.assertIs(runtime.global_child, child)
        self.assertNotIn(session.id, runtime.sessions)
        self.assertTrue(session.closed)

    async def test_status_reports_the_global_child(self):
        runtime = runtime_for("global")
        child = FakeChild()
        runtime.global_child = child
        runtime.global_owner = "abc"
        status = runtime.status()
        self.assertEqual(status["mode"], "global")
        self.assertEqual(status["children"], [{"sessionId": "abc", "pid": 4321, "idleSec": 0.0}])


if __name__ == "__main__":
    unittest.main()
