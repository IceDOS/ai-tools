"""pi-agent rate-limit hook client: communicates with pi-vpn-rotate-service daemon."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOCKET_TIMEOUT_S = 300.0


def get_socket_path():
    env_path = os.environ.get("PI_VPN_ROTATE_SOCKET")
    if env_path:
        return Path(env_path)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "pi-vpn-rotate.sock"


def ensure_service_running(sock_path):
    p = Path(sock_path)
    if p.exists():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(p))
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            try:
                p.unlink()
            except OSError:
                pass
    try:
        res = subprocess.run(
            ["systemctl", "--user", "start", "pi-vpn-rotate"],
            capture_output=True,
            timeout=5,
        )
        if res.returncode == 0:
            for _ in range(30):
                time.sleep(0.1)
                if p.exists():
                    try:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.connect(str(p))
                        s.close()
                        return True
                    except OSError:
                        pass
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def send_request(req, sock_path=None, timeout=DEFAULT_SOCKET_TIMEOUT_S):
    p = Path(sock_path) if sock_path else get_socket_path()
    if not p.exists():
        if not ensure_service_running(p):
            return {"ok": False, "reason": "service not running and could not be started"}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(p))
    except (ConnectionRefusedError, FileNotFoundError):
        s.close()
        try:
            p.unlink()
        except OSError:
            pass
        if not ensure_service_running(p):
            return {"ok": False, "reason": "service not running and could not be started"}
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(str(p))
        except (OSError, socket.timeout) as e:
            return {"ok": False, "reason": str(e)}
    except (OSError, socket.timeout) as e:
        s.close()
        return {"ok": False, "reason": str(e)}

    try:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        s.close()
        if not buf:
            return {"ok": False, "reason": "empty response from service"}
        return json.loads(buf.decode("utf-8"))
    except socket.timeout:
        s.close()
        return {"ok": False, "reason": "socket timeout"}
    except (OSError, json.JSONDecodeError) as e:
        s.close()
        return {"ok": False, "reason": str(e)}


def print_status(resp):
    if not resp.get("ok"):
        print(f"Error querying VPN status: {resp.get('reason')}", file=sys.stderr)
        return
    connected = resp.get("connected")
    if connected:
        server = resp.get("server") or "unknown"
        country = resp.get("country") or ""
        location = resp.get("location") or ""
        ip = resp.get("egressIp") or ""
        ip_str = f" [IP: {ip}]" if ip else ""
        print(f"VPN Status: Connected to {server} ({country}, {location}){ip_str}")
    else:
        print("VPN Status: Disconnected")
    if resp.get("allRateLimited"):
        refresh_in = resp.get("refreshIn", 0)
        print(f"Warning: All candidate countries are rate-limited. Next refresh in {refresh_in}s.")
    rate_limited = resp.get("rateLimitedCountries", [])
    if rate_limited:
        print("Rate-limited countries:")
        for item in rate_limited:
            print(f"  - {item['country']}: refresh in {item['refreshIn']}s ({item.get('reason', 'rate_limit')})")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    subcommand = argv[0] if argv else None

    if subcommand == "status":
        resp = send_request({"action": "status"})
        print_status(resp)
        return 0 if resp.get("ok") else 1

    if subcommand == "clear":
        resp = send_request({"action": "clear"})
        if resp.get("ok"):
            print("Rate-limit state cleared.")
            return 0
        print(f"Failed to clear state: {resp.get('reason')}", file=sys.stderr)
        return 1

    payload = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
        except Exception:
            pass

    req = {
        "action": "rotate",
        "session": payload.get("session") or payload.get("session_id", ""),
        "retry": payload.get("retry", 0),
        "errorMessage": payload.get("error_message") or payload.get("errorMessage", ""),
        "errorKind": payload.get("error_kind") or payload.get("errorKind", "rate_limit"),
        "provider": payload.get("provider", ""),
        "model": payload.get("model", ""),
    }

    resp = send_request(req)
    if resp.get("ok"):
        server = resp.get("server", "unknown")
        country = resp.get("country", "")
        print(f"Connected to {server} ({country})")
        return 0

    reason = resp.get("reason", "unknown error")
    print(f"VPN rotation failed: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
