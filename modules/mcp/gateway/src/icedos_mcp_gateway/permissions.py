"""Peer credential checks for the loopback-only gateway.

There is no token: the daemon trusts a connection only if the client socket that
talks to it belongs to the daemon's own uid. The peer's ephemeral port is looked
up in /proc/net/tcp{,6}.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Iterable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

NET_TABLES = ("net/tcp", "net/tcp6")


def _port(field: str) -> int | None:
    """Extract the port from an /proc/net/tcp `address:port` hex field."""
    _, _, raw = field.rpartition(":")
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _owners(path: str, port: int, remote_port: int | None) -> set[int]:
    owners: set[int] = set()
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return owners
    with handle:
        next(handle, None)  # header line
        for line in handle:
            fields = line.split()
            if len(fields) < 8:
                continue
            if _port(fields[1]) != port:
                continue
            if remote_port is not None and _port(fields[2]) != remote_port:
                continue
            try:
                owners.add(int(fields[7]))
            except ValueError:
                continue
    return owners


def socket_owners(
    port: int, *, remote_port: int | None = None, proc_root: str = "/proc"
) -> frozenset[int]:
    """Return the uids owning sockets with this local port."""
    owners: set[int] = set()
    for table in NET_TABLES:
        owners |= _owners(os.path.join(proc_root, table), port, remote_port)
    return frozenset(owners)


_LOOKUP = Callable[..., Iterable[int]]


class PeerUidMiddleware:
    """Reject HTTP and websocket connections whose peer socket is not ours."""

    def __init__(self, app: ASGIApp, lookup: _LOOKUP = socket_owners) -> None:
        self.app = app
        self.lookup = lookup

    def _owners(self, port: int, server_port: int | None) -> frozenset[int]:
        if server_port is None:
            owners = self.lookup(port)
        else:
            try:
                owners = self.lookup(port, remote_port=server_port)
            except TypeError:
                owners = self.lookup(port)
        return frozenset(owners)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        server = scope.get("server")
        server_port = server[1] if server else None
        owners = frozenset()
        if client is not None:
            owners = self._owners(client[1], server_port)

        if owners == {os.getuid()}:
            await self.app(scope, receive, send)
            return

        peer = client[0] if client else "unknown"
        log.warning(
            "rejected %s from %s: peer socket owned by %s, not uid %d",
            scope["type"],
            peer,
            sorted(owners) or "nobody",
            os.getuid(),
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"error": "gateway only accepts connections from the owning user"},
            status_code=403,
        )
        await response(scope, receive, send)
