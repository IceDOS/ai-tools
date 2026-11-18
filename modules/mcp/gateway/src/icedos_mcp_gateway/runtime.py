"""Session and child supervision for one fronted MCP server."""

from __future__ import annotations

import asyncio
import collections
import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TextIO

from .child import ChildProcess, Pending
from .config import ServerSpec
from .jsonrpc import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    NOTIFICATION,
    REQUEST,
    RESPONSE,
    classify,
    error_response,
    response,
)

log = logging.getLogger(__name__)

OUTBOX_SIZE = 1024

# A request may legitimately run for an hour (an icedos rebuild does), so the
# per-request timeout never drops below that floor even when idling is short.
MIN_REQUEST_TIMEOUT = 3600.0


def request_timeout_for(idle_timeout: float) -> float:
    """How long to wait for the child before answering the client with an error."""
    return max(float(idle_timeout), MIN_REQUEST_TIMEOUT)


# A session outlives its child: stopping an idle child is transparent, because the
# next request respawns it and replays the handshake. Expiring the session itself
# is a last resort, since the official Python SDK turns a 404 into "Session
# terminated" instead of initializing again, and every client would lose its tools.
SESSION_TTL_FLOOR = 86400.0


def session_ttl_for(idle_timeout: float) -> float:
    """How long a session survives without requests before requests 404."""
    return max(float(idle_timeout), SESSION_TTL_FLOOR)


class Outbox:
    """Bounded queue of server-to-client messages for one HTTP session."""

    def __init__(self, maxlen: int = OUTBOX_SIZE) -> None:
        self._items: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._event = asyncio.Event()
        self._closed = False

    def put(self, message: dict) -> None:
        if self._closed:
            return
        self._items.append(message)
        self._event.set()

    async def get(self) -> dict | None:
        """Pop the next message, or None once the outbox is closed."""
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                return None
            self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        self._closed = True
        self._event.set()


@dataclass
class Session:
    """One streamable-HTTP MCP session."""

    id: str
    server: str
    mode: str
    outbox: Outbox = field(default_factory=Outbox)
    get_stream_open: bool = False
    child: ChildProcess | None = None
    last_used: float = field(default_factory=time.monotonic)
    inflight: int = 0
    closed: bool = False
    # Cached initialize params, replayed when a reaped child is respawned.
    init: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used = time.monotonic()


def new_session(server: str, mode: str) -> Session:
    return Session(id=uuid.uuid4().hex, server=server, mode=mode)


class ServerRuntime:
    """Owns every child process and session for one configured server."""

    def __init__(
        self,
        spec: ServerSpec,
        stderr_sink: TextIO | None = None,
        request_timeout: float = MIN_REQUEST_TIMEOUT,
    ) -> None:
        self.spec = spec
        self.name = spec.name
        self.mode = spec.mode
        self.stderr_sink = stderr_sink
        self.request_timeout = request_timeout
        self.sessions: dict[str, Session] = {}
        self.global_child: ChildProcess | None = None
        self.global_init: dict | None = None
        self.global_init_result: Any = None
        self.global_owner: str | None = None
        self.lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def _spawn(self) -> ChildProcess:
        child = ChildProcess(
            self.name,
            self.spec,
            self._handle_child_message,
            self._handle_child_exit,
            stderr_sink=self.stderr_sink,
        )
        await child.start()
        return child

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await self.drop_session(session)
        child, self.global_child = self.global_child, None
        if child is not None:
            await child.stop()

    def status(self) -> dict:
        children = []
        if self.global_child is not None:
            children.append(
                {
                    "sessionId": self.global_owner or "",
                    "pid": self.global_child.pid,
                    "idleSec": round(self.global_child.idle_seconds(), 3),
                }
            )
        else:
            for session in self.sessions.values():
                child = session.child
                if child is not None:
                    children.append(
                        {
                            "sessionId": session.id,
                            "pid": child.pid,
                            "idleSec": round(child.idle_seconds(), 3),
                        }
                    )
        return {"mode": self.mode, "children": children}

    async def reap(self, idle_timeout: float, session_ttl: float | None = None) -> None:
        """Stop idle children and expire sessions past their ttl.

        Reaping a child is invisible to the client: the next request respawns it
        and replays that session's cached handshake. Only an expired session, a
        failed handshake, or DELETE turns later requests into 404s.
        """
        ttl = session_ttl_for(idle_timeout) if session_ttl is None else float(session_ttl)
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if session.closed or self._session_busy(session):
                continue
            idle = now - session.last_used
            if idle >= ttl:
                log.info("expiring idle MCP session %s for %s", session.id, self.name)
                await self.drop_session(session)
            elif idle_timeout > 0 and idle >= idle_timeout and self.mode == "per-agent":
                await self._reap_session_child(session, idle_timeout)
        if idle_timeout <= 0 or self.mode != "global":
            return
        child = self.global_child
        if (
            child is not None
            and child.inflight == 0
            and child.idle_seconds() >= idle_timeout
        ):
            log.info("reaping idle MCP server %s (pid %s)", self.name, child.pid)
            self.global_child = None
            self.global_owner = None
            await child.stop()

    async def _reap_session_child(self, session: Session, idle_timeout: float) -> None:
        """Stop one per-agent child, leaving its session (and cache) intact."""
        child = session.child
        if child is None or child.inflight > 0:
            return
        if child.idle_seconds() < idle_timeout:
            return
        log.info("reaping idle MCP server %s (pid %s)", self.name, child.pid)
        session.child = None
        await child.stop()

    def _session_busy(self, session: Session) -> bool:
        """True while a request from this session still waits for its reply."""
        return session.inflight > 0

    async def drop_session(self, session: Session) -> None:
        if session.closed:
            return
        session.closed = True
        session.outbox.close()
        self.sessions.pop(session.id, None)
        child, session.child = session.child, None
        if child is not None and self.mode == "per-agent":
            for pending in list(child.pending.values()):
                if pending.session == session.id and pending.response is None:
                    pending.response = error_response(
                        pending.client_id, INTERNAL_ERROR, "MCP session closed"
                    )
                    pending.event.set()
            if child.alive:
                await child.stop()

    # -- inbound client messages ------------------------------------------

    async def handle_request(self, session: Session, message: dict) -> dict:
        """Forward a client request and return the JSON-RPC reply."""
        session.touch()
        session.inflight += 1
        try:
            child = await self._child_for_request(session)
            if child is None:
                return error_response(
                    message.get("id"),
                    INTERNAL_ERROR,
                    f"MCP server '{self.name}' is not available",
                )
            return await self._forward(child, session, message)
        finally:
            session.inflight -= 1

    async def handle_initialize(self, session: Session, message: dict) -> dict:
        session.touch()
        session.inflight += 1
        try:
            if self.mode == "global":
                return await self._initialize_global(session, message)
            try:
                child = await self._spawn()
            except Exception as exc:
                log.warning("cannot start MCP server %s: %s", self.name, exc)
                return error_response(
                    message.get("id"),
                    INTERNAL_ERROR,
                    f"failed to start MCP server '{self.name}': {exc}",
                )
            session.child = child
            reply = await self._forward(child, session, message)
            if "error" in reply or "result" not in reply:
                await self.drop_session(session)
                return reply
            # Keep the params so a reaped child can be respawned transparently.
            session.init = message.get("params") or {}
            return reply
        finally:
            session.inflight -= 1

    async def handle_notification(self, session: Session, message: dict) -> None:
        session.touch()
        method = message.get("method")
        params = message.get("params")
        if self.mode == "global":
            child = self.global_child
            if method == "notifications/initialized":
                return  # the gateway sends its own initialized after the handshake
        else:
            child = session.child
        if child is None or not child.alive:
            return
        if method == "notifications/cancelled" and isinstance(params, dict):
            params = self._rewrite_cancel(child, session, params)
        forwarded = dict(message)
        if params is not None:
            forwarded["params"] = params
        child.post(forwarded)

    async def handle_client_response(self, session: Session, message: dict) -> None:
        session.touch()
        if self.mode != "per-agent":
            log.debug("dropping client response in global mode for %s", self.name)
            return
        child = session.child
        if child is not None and child.alive:
            child.post(message)

    def _rewrite_cancel(
        self, child: ChildProcess, session: Session, params: dict
    ) -> dict:
        """Map the client's request id onto the id the child knows."""
        wanted = params.get("requestId")
        for gateway_id, pending in child.pending.items():
            if pending.session == session.id and pending.client_id == wanted:
                return {**params, "requestId": gateway_id}
        return params

    async def _child_for_request(self, session: Session) -> ChildProcess | None:
        if self.mode == "per-agent":
            child = session.child
            if child is not None and child.alive:
                return child
            return await self._restart_session_child(session)
        return await self._ensure_global_child_locked(session)

    async def _restart_session_child(self, session: Session) -> ChildProcess | None:
        """Respawn a reaped per-agent child and replay its handshake."""
        async with session.lock:
            child = session.child
            if child is not None and child.alive:
                return child
            init = session.init
            if init is None:
                return None
            try:
                child = await self._spawn()
            except Exception as exc:
                log.warning("cannot restart MCP server %s: %s", self.name, exc)
                return None
            session.child = child
            log.info("restarted MCP server %s (pid %s), replaying initialize", self.name, child.pid)
            replay = {
                "jsonrpc": "2.0",
                "id": child.next_request_id(),
                "method": "initialize",
                "params": init,
            }
            try:
                reply = await self._exchange(child, replay, session_id=session.id)
            except asyncio.CancelledError:
                session.child = None
                await child.stop()
                raise
            if "error" in reply or "result" not in reply:
                log.warning("replaying initialize to %s failed: %s", self.name, reply.get("error"))
                session.child = None
                await child.stop()
                return None
            child.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return child

    async def _ensure_global_child_locked(self, session: Session) -> ChildProcess | None:
        async with self.lock:
            child = self.global_child
            if child is not None and child.alive:
                return child
            if self.global_init is None:
                return None
            try:
                child = await self._spawn()
            except Exception as exc:
                log.warning("cannot restart MCP server %s: %s", self.name, exc)
                return None
            self.global_child = child
            self.global_owner = session.id
            log.info("restarted MCP server %s with cached initialize", self.name)
            replay = {
                "jsonrpc": "2.0",
                "id": child.next_request_id(),
                "method": "initialize",
                "params": self.global_init,
            }
            try:
                reply = await self._exchange(child, replay, session_id="")
            except asyncio.CancelledError:
                self.global_child = None
                self.global_owner = None
                await child.stop()
                raise
            if "error" in reply or "result" not in reply:
                log.warning(
                    "replaying initialize to %s failed: %s", self.name, reply.get("error")
                )
                self.global_child = None
                self.global_owner = None
                await child.stop()
                return None
            child.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return child

    async def _initialize_global(self, session: Session, message: dict) -> dict:
        async with self.lock:
            if self.global_init_result is not None:
                # Echo the caller's version so mixed client versions can share one child.
                cached = copy.deepcopy(self.global_init_result)
                params = message.get("params")
                requested = params.get("protocolVersion") if isinstance(params, dict) else None
                if isinstance(requested, str) and requested:
                    cached["protocolVersion"] = requested
                return response(message.get("id"), cached)
            try:
                child = await self._spawn()
            except Exception as exc:
                log.warning("cannot start MCP server %s: %s", self.name, exc)
                return error_response(
                    message.get("id"),
                    INTERNAL_ERROR,
                    f"failed to start MCP server '{self.name}': {exc}",
                )
            self.global_child = child
            self.global_owner = session.id
            try:
                reply = await self._forward(child, session, message)
            except asyncio.CancelledError:
                # The client went away mid-handshake; do not keep a half-built child.
                self.global_child = None
                self.global_owner = None
                await child.stop()
                raise
            if "error" in reply or "result" not in reply:
                self.global_child = None
                self.global_owner = None
                await child.stop()
                return reply
            self.global_init = message.get("params") or {}
            self.global_init_result = reply["result"]
            child.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return reply

    async def _forward(self, child: ChildProcess, session: Session, message: dict) -> dict:
        return await self._exchange(child, message, session_id=session.id)

    async def _exchange(
        self, child: ChildProcess, message: dict, session_id: str
    ) -> dict:
        client_id = message.get("id")
        gateway_id = child.next_request_id()
        pending = Pending(session=session_id, client_id=client_id)
        child.pending[gateway_id] = pending
        child.inflight += 1
        try:
            forwarded = dict(message)
            forwarded["id"] = gateway_id
            child.post(forwarded)
            try:
                await asyncio.wait_for(pending.event.wait(), self.request_timeout)
            except asyncio.TimeoutError:
                # Dropping the pending entry in the finally block frees the child
                # to be reaped even though it never answered.
                log.warning(
                    "MCP server %s did not answer %r in %ss",
                    self.name,
                    message.get("method"),
                    self.request_timeout,
                )
                return error_response(
                    client_id,
                    INTERNAL_ERROR,
                    f"MCP server '{self.name}' did not answer within {self.request_timeout:g}s",
                )
            return pending.response or error_response(
                client_id, INTERNAL_ERROR, "no response from MCP server"
            )
        finally:
            child.pending.pop(gateway_id, None)
            child.inflight -= 1
            child.touch()

    # -- child callbacks ---------------------------------------------------

    def _session_for_child(self, child: ChildProcess) -> Session | None:
        for session in self.sessions.values():
            if session.child is child:
                return session
        return None

    async def _handle_child_message(self, child: ChildProcess, message: dict) -> None:
        kind = classify(message)
        if kind == RESPONSE:
            self._deliver_response(child, message)
            return
        if kind == REQUEST:
            if self.mode == "per-agent":
                session = self._session_for_child(child)
                if session is not None:
                    session.outbox.put(message)
                return
            child.post(
                error_response(
                    message.get("id"),
                    METHOD_NOT_FOUND,
                    f"MCP server '{self.name}' cannot send requests in global mode",
                )
            )
            return
        if kind == NOTIFICATION:
            if self.mode == "per-agent":
                session = self._session_for_child(child)
                if session is not None:
                    session.outbox.put(message)
                return
            for session in list(self.sessions.values()):
                if not session.closed:
                    session.outbox.put(message)
            return
        log.debug("ignoring unusable message from MCP server %s: %.120r", self.name, message)

    def _deliver_response(self, child: ChildProcess, message: dict) -> None:
        pending = child.pending.get(str(message.get("id")))
        if pending is None:
            log.debug("no pending request for %r on %s", message.get("id"), self.name)
            return
        if "error" in message and "result" not in message:
            reply = {"jsonrpc": "2.0", "id": pending.client_id, "error": message["error"]}
        else:
            reply = {"jsonrpc": "2.0", "id": pending.client_id, "result": message.get("result")}
        pending.response = reply
        pending.event.set()

    async def _handle_child_exit(self, child: ChildProcess, code: int | None) -> None:
        message = f"MCP server '{self.name}' exited (code {code})"
        if child.closing:
            log.info("%s", message)
        else:
            log.warning("%s", message)
        for pending in list(child.pending.values()):
            pending.response = error_response(pending.client_id, INTERNAL_ERROR, message)
            pending.event.set()
        if self.global_child is child:
            self.global_child = None
            self.global_owner = None
        for session in list(self.sessions.values()):
            if session.child is child:
                # A crash is as invisible as a reap: keep the session (and its
                # cached handshake) so the next request respawns and replays.
                # Requests in flight already got the error reply above.
                session.child = None
        child.cleanup()
