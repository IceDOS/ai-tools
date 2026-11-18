"""Starlette application exposing each configured server at /<name>."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Callable, Iterable, Mapping

from mcp.server.streamable_http import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_SSE,
    MCP_SESSION_ID_HEADER,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import GatewayConfig
from .jsonrpc import (
    INVALID_REQUEST,
    NOTIFICATION,
    PARSE_ERROR,
    REQUEST,
    classify,
    error_response,
)
from .permissions import PeerUidMiddleware, socket_owners
from .runtime import (
    ServerRuntime,
    Session,
    new_session,
    request_timeout_for,
    session_ttl_for,
)

log = logging.getLogger(__name__)

PING_INTERVAL = 15.0
SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}


def _json(status: int, message: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(message, status_code=status, headers=dict(headers or {}))


def create_app(
    config: GatewayConfig,
    peer_lookup: Callable[..., Iterable[int]] | None = None,
    reap_interval: float = 1.0,
    stderr_sink: Any = None,
    request_timeout: float | None = None,
    session_ttl: float | None = None,
) -> Starlette:
    """Build the gateway application for a validated configuration."""
    timeout = (
        request_timeout_for(config.idle_timeout)
        if request_timeout is None
        else float(request_timeout)
    )
    ttl = (
        session_ttl_for(config.idle_timeout)
        if session_ttl is None
        else float(session_ttl)
    )
    runtimes = {
        name: ServerRuntime(spec, stderr_sink=stderr_sink, request_timeout=timeout)
        for name, spec in config.servers.items()
    }

    def find_session(session_id: str, name: str) -> tuple[ServerRuntime | None, Session | None]:
        runtime = runtimes.get(name)
        if runtime is None:
            return None, None
        session = runtime.sessions.get(session_id)
        return runtime, session

    # -- workers -----------------------------------------------------------

    async def reap_loop() -> None:
        while True:
            await asyncio.sleep(reap_interval)
            for runtime in list(runtimes.values()):
                try:
                    await runtime.reap(config.idle_timeout, ttl)
                except Exception:  # pragma: no cover - defensive
                    log.exception("reaping %s failed", runtime.name)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        reaper = None
        if reap_interval and reap_interval > 0:
            reaper = asyncio.create_task(reap_loop(), name="mcp-gateway-reaper")
        try:
            yield
        finally:
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reaper
            for runtime in list(runtimes.values()):
                with contextlib.suppress(Exception):
                    await runtime.shutdown()

    # -- responses ---------------------------------------------------------

    def status() -> Response:
        return JSONResponse(
            {"servers": {name: runtime.status() for name, runtime in runtimes.items()}}
        )

    def reply(request: Request, message: dict, headers: Mapping[str, str] | None = None) -> Response:
        """Answer a request as JSON, or as one SSE event for SSE-only clients."""
        accepts = request.headers.get("accept", "")
        if "application/json" in accepts or CONTENT_TYPE_SSE not in accepts:
            return _json(200, message, headers)
        payload = f"event: message\ndata: {json.dumps(message)}\n\n"
        return Response(
            payload, media_type=CONTENT_TYPE_SSE, headers=dict(headers or {})
        )

    def sse_stream(session: Session) -> StreamingResponse:
        async def events():
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(session.outbox.get(), PING_INTERVAL)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if message is None:
                        break
                    yield f"event: message\ndata: {json.dumps(message)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                session.get_stream_open = False

        return StreamingResponse(
            events(), media_type=CONTENT_TYPE_SSE, headers=dict(SSE_HEADERS)
        )

    # -- disconnect handling -----------------------------------------------

    async def run_until_disconnect(request: Request, awaitable: Any) -> Any | None:
        """Run a handler, abandoning it once the client is gone.

        A client that disappears mid-request would otherwise leave the child
        request pending forever, so ``inflight`` never drops and the child can
        never be reaped. The receive channel is watched directly: Starlette's
        ``Request.is_disconnected`` relies on an anyio cancel scope, which can
        swallow a later cancellation of the same task.
        """
        work = asyncio.ensure_future(awaitable)

        async def watcher_coro() -> bool:
            message = await request.receive()
            return isinstance(message, dict) and message.get("type") == "http.disconnect"

        watcher = asyncio.ensure_future(watcher_coro())
        gone = False
        try:
            await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher.done() and not watcher.cancelled():
                with contextlib.suppress(Exception):
                    gone = watcher.result()
            if gone and not work.done():
                work.cancel()
        finally:
            for task in (work, watcher):
                if not task.done():
                    task.cancel()
            for task in (work, watcher):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if gone or work.cancelled():
            return None
        error = work.exception()
        if error is not None:
            raise error
        return work.result()

    # -- handlers ----------------------------------------------------------

    async def handle_status(request: Request) -> Response:
        if request.method != "GET":
            return Response(status_code=405)
        return status()

    async def handle_post(request: Request, name: str) -> Response:
        runtime = runtimes.get(name)
        if runtime is None:
            return JSONResponse({"error": f"unknown server {name!r}"}, status_code=404)

        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != CONTENT_TYPE_JSON:
            return _json(
                415,
                error_response(
                    None, INVALID_REQUEST, f"unsupported content type {content_type!r}"
                ),
            )
        try:
            message = json.loads(await request.body())
        except ValueError:
            return _json(400, error_response(None, PARSE_ERROR, "Parse error"))
        if isinstance(message, list):
            return _json(
                400,
                error_response(None, INVALID_REQUEST, "JSON-RPC batches are not supported"),
            )
        if not isinstance(message, dict):
            return _json(400, error_response(None, INVALID_REQUEST, "Invalid Request"))
        kind = classify(message)
        if kind is None:
            return _json(
                400, error_response(message.get("id"), INVALID_REQUEST, "Invalid Request")
            )

        if kind == REQUEST and message.get("method") == "initialize":
            session = new_session(runtime.name, runtime.mode)
            runtime.sessions[session.id] = session
            log.debug("new %s session %s for %s", runtime.mode, session.id, name)
            answer = await run_until_disconnect(
                request, runtime.handle_initialize(session, message)
            )
            if answer is None:
                await runtime.drop_session(session)
                return Response(status_code=204)
            if "error" in answer:
                await runtime.drop_session(session)
                return reply(request, answer)
            return reply(request, answer, {MCP_SESSION_ID_HEADER: session.id})

        session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        if not session_id:
            return _json(
                400,
                error_response(
                    message.get("id"), INVALID_REQUEST, f"missing {MCP_SESSION_ID_HEADER} header"
                ),
            )
        runtime, session = find_session(session_id, name)
        if session is None or session.closed:
            return _json(
                404,
                error_response(message.get("id"), INVALID_REQUEST, "Session terminated"),
            )
        session.touch()

        if kind == REQUEST:
            answer = await run_until_disconnect(
                request, runtime.handle_request(session, message)
            )
            if answer is None:
                # The client vanished; the runtime already dropped the pending
                # child request, so this child becomes reapable again.
                return Response(status_code=204)
            return reply(request, answer)
        if kind == NOTIFICATION:
            await runtime.handle_notification(session, message)
            return Response(status_code=202)
        await runtime.handle_client_response(session, message)
        return Response(status_code=202)

    async def handle_get(request: Request, name: str) -> Response:
        runtime = runtimes.get(name)
        if runtime is None:
            return JSONResponse({"error": f"unknown server {name!r}"}, status_code=404)
        session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        if not session_id:
            return _json(
                400,
                error_response(None, INVALID_REQUEST, f"missing {MCP_SESSION_ID_HEADER} header"),
            )
        runtime, session = find_session(session_id, name)
        if session is None or session.closed:
            return _json(404, error_response(None, INVALID_REQUEST, "Session terminated"))
        if session.get_stream_open:
            return _json(
                409,
                error_response(None, INVALID_REQUEST, "an SSE stream is already open"),
            )
        session.get_stream_open = True
        session.touch()
        return sse_stream(session)

    async def handle_delete(request: Request, name: str) -> Response:
        if name not in runtimes:
            return JSONResponse({"error": f"unknown server {name!r}"}, status_code=404)
        session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        if not session_id:
            return _json(
                400,
                error_response(None, INVALID_REQUEST, f"missing {MCP_SESSION_ID_HEADER} header"),
            )
        runtime, session = find_session(session_id, name)
        if session is None:
            return _json(404, error_response(None, INVALID_REQUEST, "Session terminated"))
        await runtime.drop_session(session)
        return Response(status_code=204)

    async def endpoint(request: Request) -> Response:
        name = request.url.path.strip("/")
        if not name:
            return await handle_status(request)
        if request.method == "POST":
            return await handle_post(request, name)
        if request.method == "GET":
            return await handle_get(request, name)
        if request.method == "DELETE":
            return await handle_delete(request, name)
        return Response(status_code=405)

    lookup = socket_owners if peer_lookup is None else peer_lookup

    return Starlette(
        routes=[
            Route("/", endpoint, methods=["GET", "POST", "DELETE"]),
            Route("/{path:path}", endpoint, methods=["GET", "POST", "DELETE"]),
        ],
        middleware=[Middleware(PeerUidMiddleware, lookup=lookup)],
        lifespan=lifespan,
    )
