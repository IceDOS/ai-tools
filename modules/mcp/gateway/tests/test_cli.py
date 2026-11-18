"""Command line behaviour, run as a real subprocess."""

import asyncio
import json
import os
import signal
import socket
import tempfile
import unittest
from pathlib import Path

import httpx

import support
from icedos_mcp_gateway.cli import build_parser, is_loopback

ENV = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LoopbackTest(unittest.TestCase):
    def test_loopback_hosts(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertFalse(is_loopback("no-such-host.invalid"))

    def test_parser_requires_config(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])


class CliTest(unittest.IsolatedAsyncioTestCase):
    async def run_gateway(self, *args: str):
        proc = await asyncio.create_subprocess_exec(
            support.PYTHON,
            "-m",
            "icedos_mcp_gateway",
            *args,
            env=ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
        return proc.returncode, stdout.decode(), stderr.decode()

    async def write_config(self, tmp: str, **overrides) -> str:
        document = support.config_dict(**overrides)
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(document))
        return str(path)

    async def test_missing_config_exits_2(self):
        code, _, err = await self.run_gateway("--config", "/nonexistent/mcp.json")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    async def test_non_loopback_host_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = await self.write_config(tmp, host="0.0.0.0", servers={})
            code, _, err = await self.run_gateway("--config", path)
        self.assertEqual(code, 2)
        self.assertIn("refusing to listen", err)

    async def test_serves_status_and_stops_on_sigterm(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            path = await self.write_config(tmp, port=port)
            proc = await asyncio.create_subprocess_exec(
                support.PYTHON,
                "-m",
                "icedos_mcp_gateway",
                "--config",
                path,
                env=ENV,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:

                    async def ready():
                        try:
                            return (await client.get("/")).status_code == 200
                        except httpx.HTTPError:
                            return False

                    await support.wait_for(ready, timeout=30)
                    payload = (await client.get("/")).json()
                    self.assertEqual(payload["servers"]["fake"]["mode"], "per-agent")
                    self.assertEqual(payload["servers"]["fake"]["children"], [])
            finally:
                proc.send_signal(signal.SIGTERM)
                code = await asyncio.wait_for(proc.wait(), 30)
                await proc.communicate()
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
