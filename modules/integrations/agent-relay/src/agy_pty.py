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


class InputState:
    def __init__(self):
        self.dirty = False
        self.bracketed = False
        self._tail = b""

    def user(self, data: bytes) -> None:
        # Key sequences (arrows, function keys) arrive in one read and do not change the draft.
        if data.startswith(b"\x1b"):
            return
        for b in data:
            if b in (0x0D, 0x0A, 0x03, 0x15):
                self.dirty = False
            elif b >= 0x20 and b != 0x7F:
                self.dirty = True

    def agy(self, data: bytes) -> None:
        buf = self._tail + data
        on, off = buf.rfind(BP_ON), buf.rfind(BP_OFF)
        if on != -1 or off != -1:
            self.bracketed = on > off
        self._tail = buf[-(len(BP_ON) - 1):]


def encode_paste(text: str, bracketed: bool) -> bytes:
    if bracketed:
        return PASTE_START + text.encode() + PASTE_END
    # Without bracketed paste every newline would submit a separate prompt.
    return " ".join(text.split()).encode()


def write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def copy_winsize(src: int, dst: int) -> None:
    try:
        fcntl.ioctl(dst, termios.TIOCSWINSZ, fcntl.ioctl(src, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def handle(conn: socket.socket, state: InputState, master: int) -> None:
    conn.settimeout(1)
    try:
        req = json.loads(conn.makefile("rb").readline() or b"{}")
        text = req.get("paste") if isinstance(req, dict) else None
        if not isinstance(text, str) or not text:
            reply = "error"
        elif state.dirty:
            reply = "dirty"
        else:
            write_all(master, encode_paste(text, state.bracketed))
            time.sleep(ENTER_DELAY)
            write_all(master, b"\r")
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
                    write_all(master, data)
                else:
                    fds.remove(stdin)
            if server in ready:
                conn, _ = server.accept()
                handle(conn, state, master)
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
