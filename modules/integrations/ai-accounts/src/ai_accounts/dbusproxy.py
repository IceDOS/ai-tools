"""A session bus for agy that hides the Secret Service, so it stores its token in a file."""
import os
import select
import subprocess
from pathlib import Path


class ProxyError(Exception):
    pass


def start(bus: str, sock: Path, talk: list, proxy: str = "xdg-dbus-proxy", timeout: float = 3.0) -> int:
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    r, w = os.pipe()
    argv = [proxy, f"--fd={w}", bus, str(sock), "--filter", *(f"--talk={n}" for n in talk)]
    try:
        subprocess.Popen(argv, pass_fds=(w,), start_new_session=True)
    except OSError as e:
        os.close(r)
        os.close(w)
        raise ProxyError(f"cannot start {proxy}: {e}") from e
    os.close(w)
    ready, _, _ = select.select([r], [], [], timeout)
    if not ready or not os.read(r, 1):
        os.close(r)
        raise ProxyError(f"{proxy} did not become ready within {timeout}s")
    # agy inherits r; when agy exits the pipe closes and the proxy stops.
    os.set_inheritable(r, True)
    return r
