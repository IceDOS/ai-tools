"""A child MCP server process spoken to over stdio."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TextIO

log = logging.getLogger(__name__)

STOP_TIMEOUT = 2.0
READ_CHUNK = 65536


@dataclass
class Pending:
    """A gateway request waiting for the child's reply."""

    session: str
    client_id: Any
    event: asyncio.Event = field(default_factory=asyncio.Event)
    response: dict[str, Any] | None = None


class ChildProcess:
    """Spawn and pump one stdio MCP server."""

    def __init__(
        self,
        name: str,
        spec: Any,
        on_message: Callable[[ChildProcess, dict], Awaitable[None]],
        on_exit: Callable[[ChildProcess, int | None], Awaitable[None]],
        stderr_sink: TextIO | None = None,
    ) -> None:
        self.name = name
        self.spec = spec
        self.on_message = on_message
        self.on_exit = on_exit
        self.stderr_sink = sys.stderr if stderr_sink is None else stderr_sink
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.started = time.monotonic()
        self.last_used = self.started
        self.inflight = 0
        self.pending: dict[str, Pending] = {}
        self.closing = False
        self._queue: asyncio.Queue[dict | None] | None = asyncio.Queue()
        self._seq = 0
        self._readers: list[asyncio.Task] = []
        self._watch: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the child; raises if exec fails."""
        env = {**os.environ, **self.spec.env}
        self.started = self.last_used = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            self.spec.command,
            *self.spec.args,
            env=env,
            cwd=os.path.expanduser("~"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.pid = self.proc.pid
        log.info("started MCP server %s (pid %d)", self.name, self.pid)
        self._readers = [
            asyncio.create_task(self._stdin_pump(), name=f"{self.name}-stdin"),
            asyncio.create_task(self._stdout_pump(), name=f"{self.name}-stdout"),
            asyncio.create_task(self._stderr_pump(), name=f"{self.name}-stderr"),
        ]
        self._watch = asyncio.create_task(self._watch_exit(), name=f"{self.name}-watch")

    async def stop(self) -> None:
        """Shut the child down; never raises."""
        if self.closing:
            return
        self.closing = True
        queue, self._queue = self._queue, None
        if queue is not None:
            queue.put_nowait(None)

        proc = self.proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                log.info("stopping MCP server %s (pid %s) with SIGTERM", self.name, self.pid)
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
                except (asyncio.TimeoutError, Exception):
                    log.warning("killing MCP server %s (pid %s)", self.name, self.pid)
                    self._signal(signal.SIGKILL)
                    with contextlib.suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)

        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task() and not watch.done():
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(watch, STOP_TIMEOUT)
        self.cleanup()

    def cleanup(self) -> None:
        """Cancel the reader pumps (safe to call more than once)."""
        readers, self._readers = self._readers, []
        for task in readers:
            task.cancel()

    def _signal(self, sig: int) -> None:
        if self.pid is None:
            return
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            proc = self.proc
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.send_signal(sig)

    # -- state -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return (
            not self.closing
            and self.proc is not None
            and self.proc.returncode is None
        )

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    # -- io ----------------------------------------------------------------

    def next_request_id(self) -> str:
        self._seq += 1
        return f"gw-{self._seq}"

    def post(self, message: dict) -> None:
        """Queue a message for the child (non-blocking)."""
        queue = self._queue
        if self.closing or queue is None or not self.alive:
            return
        self.touch()
        queue.put_nowait(message)

    async def _stdin_pump(self) -> None:
        queue = self._queue
        proc = self.proc
        if queue is None or proc is None or proc.stdin is None:
            return
        while True:
            message = await queue.get()
            if message is None:
                break
            data = json.dumps(message).encode() + b"\n"
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                log.debug("stdin of MCP server %s is closed", self.name)
                break
            except Exception:
                log.exception("failed writing to MCP server %s", self.name)
                break

    async def _stdout_pump(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        await self._pump_lines(proc.stdout, self._handle_stdout_line)

    async def _stderr_pump(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        await self._pump_lines(proc.stderr, self._handle_stderr_line)

    async def _pump_lines(self, stream: asyncio.StreamReader, handler) -> None:
        buffer = b""
        while True:
            try:
                chunk = await stream.read(READ_CHUNK)
            except (asyncio.CancelledError, ConnectionResetError):
                raise
            except Exception:
                log.debug("read from MCP server %s failed", self.name, exc_info=True)
                return
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                await handler(line)
        if buffer.strip():
            await handler(buffer)

    async def _handle_stdout_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            log.debug("dropping non-JSON output from %s: %.200r", self.name, text)
            return
        if not isinstance(message, dict):
            log.debug("dropping non-object message from %s", self.name)
            return
        self.touch()
        try:
            await self.on_message(self, message)
        except Exception:
            log.exception("failed handling a message from MCP server %s", self.name)

    async def _handle_stderr_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            return
        try:
            self.stderr_sink.write(f"[{self.name}] {text}\n")
            self.stderr_sink.flush()
        except Exception:
            log.debug("stderr sink for %s failed", self.name, exc_info=True)

    async def _watch_exit(self) -> None:
        proc = self.proc
        if proc is None:
            return
        code = await proc.wait()
        log.info("MCP server %s (pid %s) exited with code %s", self.name, self.pid, code)
        try:
            await self.on_exit(self, code)
        except Exception:
            log.exception("failed handling the exit of MCP server %s", self.name)
        self.cleanup()
