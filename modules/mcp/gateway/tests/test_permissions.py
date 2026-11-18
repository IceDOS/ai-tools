"""Peer uid checks."""

import os
import tempfile
import unittest
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from icedos_mcp_gateway.permissions import PeerUidMiddleware, socket_owners

HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def write_table(root: Path, name: str, rows: list[str]) -> None:
    net = root / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / name).write_text(HEADER + "".join(row + "\n" for row in rows))


def row(local: str, remote: str, uid: int) -> str:
    return (
        f"   0: {local} {remote} 01 00000000:00000000 00:00000000 00000000"
        f" {uid:10d}        0 1 1 0000000000000000 0 0 0 0 0"
    )


class SocketOwnersTest(unittest.TestCase):
    def test_parses_tcp_and_tcp6(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_table(
                root,
                "tcp",
                [
                    row("0100007F:1E5C", "0100007F:1F90", 1000),
                    row("0100007F:1E5C", "0100007F:1F91", 1000),
                    row("0100007F:0D05", "0100007F:1F90", 1000),
                ],
            )
            write_table(
                root,
                "tcp6",
                [
                    row("00000000000000000000000001000000:1E5C", "00000000000000000000000001000000:1F90", 4242),
                ],
            )
            self.assertEqual(socket_owners(0x1E5C, proc_root=str(root)), frozenset({1000, 4242}))
            self.assertEqual(
                socket_owners(0x1E5C, remote_port=0x1F90, proc_root=str(root)),
                frozenset({1000, 4242}),
            )
            self.assertEqual(
                socket_owners(0x1E5C, remote_port=0x1F91, proc_root=str(root)),
                frozenset({1000}),
            )
            self.assertEqual(socket_owners(0x0D05, remote_port=0x1F90, proc_root=str(root)), frozenset({1000}))

    def test_missing_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(socket_owners(1234, proc_root=tmp), frozenset())

    def test_bad_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_table(root, "tcp", ["garbage", row("0100007F:1E5C", "0100007F:1F90", 1000)])
            self.assertEqual(socket_owners(0x1E5C, proc_root=str(root)), frozenset({1000}))


def app_with(owners, calls=None):
    def lookup(port, remote_port=None):
        if calls is not None:
            calls.append((port, remote_port))
        return frozenset(owners)

    async def index(request):
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[Route("/", index)],
        middleware=[Middleware(PeerUidMiddleware, lookup=lookup)],
    )


class MiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def get(self, app, path="/"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            return await client.get(path)

    async def test_accepts_own_uid(self):
        calls = []
        response = await self.get(app_with({os.getuid()}, calls))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0][0], 123)
        # httpx' ASGI transport leaves scope["server"] unset, so no remote port is probed
        self.assertIsNone(calls[0][1])

    async def test_rejects_foreign_uid(self):
        response = await self.get(app_with({os.getuid() + 1}))
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.json())

    async def test_rejects_unknown_owner(self):
        response = await self.get(app_with(set()))
        self.assertEqual(response.status_code, 403)

    async def test_passes_through_lifespan(self):
        app = app_with({os.getuid()})
        async with app.router.lifespan_context(app):
            pass
