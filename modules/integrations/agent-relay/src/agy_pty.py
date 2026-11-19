"""PTY proxy for agy so agent-relay can type relay messages into it while it is idle."""
import fcntl
import json
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import termios
import time
import tty

PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
BP_ON, BP_OFF = b"\x1b[?2004h", b"\x1b[?2004l"
ENTER_DELAY = 0.15
# Measured: a working turn redraws ~14x/s and its longest gap was 2.45s, so 2s only means "no chunk
# this instant"; callers that need real certainty read silentMs and pick their own threshold.
IDLE_SECONDS = 2.0


class InputState:
    def __init__(self):
        self.draft = 0
        self.bracketed = False
        self.last_output = time.monotonic()
        self._tail = b""

    @property
    def dirty(self) -> bool:
        return self.draft > 0

    def idle(self) -> bool:
        return time.monotonic() - self.last_output >= IDLE_SECONDS

    def silent_ms(self) -> int:
        return int((time.monotonic() - self.last_output) * 1000)

    def user(self, data: bytes) -> None:
        # A pasted block is draft text even though it starts with ESC.
        if data.startswith(PASTE_START):
            data = data[len(PASTE_START):].replace(PASTE_END, b"").replace(b"\r", b" ").replace(b"\n", b" ")
        # Key sequences (arrows, function keys) arrive in one read and do not change the draft.
        elif data.startswith(b"\x1b"):
            if data == b"\x1b":
                self.draft = 0  # agy clears its composer on a bare Esc
            return
        for b in data:
            if b in (0x0D, 0x0A, 0x03, 0x15):
                self.draft = 0
            elif b == 0x7F:
                self.draft = max(0, self.draft - 1)
            elif 0x80 <= b <= 0xBF:
                continue  # UTF-8 continuation: the character was counted on its lead byte
            elif b >= 0x20:
                self.draft += 1

    def agy(self, data: bytes) -> None:
        self.last_output = time.monotonic()
        buf = self._tail + data
        on, off = buf.rfind(BP_ON), buf.rfind(BP_OFF)
        if on != -1 or off != -1:
            self.bracketed = on > off
        self._tail = buf[-(len(BP_ON) - 1):]


def encode_paste(text: str, bracketed: bool) -> bytes:
    # Relay text is untrusted: an ESC could end the paste early and the rest would run as keystrokes.
    text = "".join(c for c in text if c in "\n\t" or not (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0))
    if bracketed:
        return PASTE_START + text.encode() + PASTE_END
    # Without bracketed paste every newline would submit a separate prompt.
    return " ".join(text.split()).encode()


def write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def feed(master: int, data: bytes, state: InputState, out: int) -> None:
    """Write to agy while forwarding its output, so a pty full in both directions cannot deadlock."""
    # A blocking tty write waits in the kernel until every byte fits, whatever select said.
    os.set_blocking(master, False)
    try:
        while data:
            r, w, _ = select.select([master], [master], [])
            if master in r:
                try:
                    chunk = os.read(master, 65536)
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    raise OSError("agy closed the pty")
                if chunk:
                    state.agy(chunk)
                    try:
                        write_all(out, chunk)
                    except OSError:
                        pass  # the terminal is gone; agy still gets the paste
            if master in w:
                try:
                    data = data[os.write(master, data):]
                except BlockingIOError:
                    pass
    finally:
        os.set_blocking(master, True)


def copy_winsize(src: int, dst: int) -> None:
    try:
        fcntl.ioctl(dst, termios.TIOCSWINSZ, fcntl.ioctl(src, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def handle(conn: socket.socket, state: InputState, master: int, out: int) -> None:
    conn.settimeout(1)
    try:
        req = json.loads(conn.makefile("rb").readline() or b"{}")
        if isinstance(req, dict) and req.get("status"):
            # No paste can be in flight here: handle() runs inside the single select loop.
            conn.sendall(json.dumps({"idle": state.idle(), "dirty": state.dirty, "silentMs": state.silent_ms()}).encode() + b"\n")
            return
        text = req.get("paste") if isinstance(req, dict) else None
        if not isinstance(text, str) or not text:
            reply = "error"
        elif state.dirty:
            reply = "dirty"
        else:
            feed(master, encode_paste(text, state.bracketed), state, out)
            time.sleep(ENTER_DELAY)
            feed(master, b"\r", state, out)
            reply = "ok"
        conn.sendall(reply.encode() + b"\n")
    except (OSError, ValueError):
        pass
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: agent-relay-agy <command> [args...]", file=sys.stderr)
        return 2
    run_dir = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "agent-relay", "pty")
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    is_tty = os.isatty(stdin)

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    if is_tty:
        copy_winsize(stdin, master)

    sock_path = os.path.join(run_dir, f"{pid}.sock")
    log_path = os.path.join(run_dir, f"{pid}.log")
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    os.chmod(sock_path, 0o600)
    server.listen(4)

    watcher = None
    if watch := os.environ.get("AGENT_RELAY_AGY_WATCH"):
        with open(log_path, "ab") as log:
            watcher = subprocess.Popen(
                [watch, str(pid), sock_path],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )

    if is_tty:
        signal.signal(signal.SIGWINCH, lambda *_: copy_winsize(stdin, master))
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda s, _: os.kill(pid, s))

    old = termios.tcgetattr(stdin) if is_tty else None
    state = InputState()
    fds = [master, stdin, server]
    try:
        if is_tty:
            tty.setraw(stdin)
        while True:
            ready, _, _ = select.select(fds, [], [])
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                state.agy(data)
                try:
                    write_all(stdout, data)
                except OSError:
                    break  # the terminal is gone, treat it like EOF
            if stdin in ready:
                try:
                    data = os.read(stdin, 65536)
                except OSError:
                    data = b""
                if data:
                    state.user(data)
                    feed(master, data, state, stdout)
                else:
                    fds.remove(stdin)
            if server in ready:
                conn, _ = server.accept()
                handle(conn, state, master, stdout)
    finally:
        if old is not None:
            try:
                termios.tcsetattr(stdin, termios.TCSAFLUSH, old)
            except termios.error:
                pass
        server.close()
        if watcher:
            watcher.terminate()
        for f in (sock_path, log_path):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
