"""pi-agent hooks.rateLimit command: rotate the ProtonVPN server; exit 0 means retry now."""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEBOUNCE_S = 10
LOCK_WAIT_S = 150
PROTON_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 90
HOUR_S = 3600
LOG_LINES = 200
COUNTRY_RE = re.compile(r"^(.+?)\s+([A-Z]{2})\s*$")
SERVER_RE = re.compile(r"^Server:\s*(\S+)\s+in\s+(.+?)\s*$", re.M)
CONNECTED_RE = re.compile(r"Connected to (\S+) in (.+?)\.\s*$", re.M)
IP_RE = re.compile(r"Your new IP address is ([0-9.]+)\.", re.M)
# pi's non-retryable account limits; a new egress IP does not clear them.
USAGE_LIMIT_RE = re.compile(
    r"GoUsageLimitError|Monthly usage limit|available balance"
    r"|insufficient_quota|out of budget|quota exceeded|billing",
    re.I,
)


class Log:
    def __init__(self, path, now):
        self.path = path
        self.now = now

    def __call__(self, text):
        print(f"pi-vpn-rotate: {text}", file=sys.stderr)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.now()))
        try:
            old = self.path.read_text().splitlines() if self.path.exists() else []
            self.path.write_text("\n".join((old + [f"{stamp} {text}"])[-LOG_LINES:]) + "\n")
        except OSError:
            pass


def run(argv, timeout):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return out.returncode, f"{out.stdout}\n{out.stderr}"


def parse_countries(text):
    codes = []
    for line in text.splitlines():
        match = COUNTRY_RE.match(line)
        if match:
            codes.append(match.group(2))
    return codes


def parse_status(text):
    if not re.search(r"^Status:\s*Connected", text, re.M):
        return None
    match = SERVER_RE.search(text)
    return (match.group(1), match.group(2)) if match else ("", "")


def next_country(countries, current, failed_target=None):
    if not countries:
        return None
    start = failed_target if (failed_target and failed_target in countries and failed_target != current) else current
    if start in countries:
        cand = countries[(countries.index(start) + 1) % len(countries)]
        if cand == current:
            cand = countries[(countries.index(cand) + 1) % len(countries)]
        return cand if cand != current else None
    return countries[0] if countries[0] != current else None


def acquire(lock, now, sleep):
    try:
        lock_file = open(lock, "a+")
    except OSError:
        return None
    deadline = now() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            if now() >= deadline:
                lock_file.close()
                return None
            sleep(0.5)


def read_shared(path):
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_shared(path, record):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, path)


def recent_rotations(record, now_s):
    cutoff = (now_s - HOUR_S) * 1000
    return [t for t in (record or {}).get("rotations", []) if isinstance(t, (int, float)) and t >= cutoff]


def decide(record, failed_at, message, cooldown_s, max_per_hour):
    """None means rotate; otherwise (exit code, reason)."""
    if USAGE_LIMIT_RE.search(message or ""):
        return 1, "account limit"
    if not record:
        return None
    last_success = record.get("at", 0) / 1000
    # Sessions share one tunnel and usually hit the same limit together.
    if last_success >= failed_at - DEBOUNCE_S:
        return 0, "debounced"
    last_attempt = record.get("attemptAt", record.get("at", 0)) / 1000
    if failed_at - last_attempt < cooldown_s:
        return 1, "cool-down"
    if len(recent_rotations(record, failed_at)) >= max_per_hour:
        return 1, "hourly cap"
    return None


def fetch_egress_ip(timeout=4):
    try:
        import urllib.request
        req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/7.88.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ip = resp.read().decode("utf-8").strip()
            if re.match(r"^[0-9.]+$", ip):
                return ip
    except Exception:
        pass
    return None


def rotate(state, record, run, now, protonvpn, log, countries_allowlist=None, has_iface=os.path.exists, get_ip=fetch_egress_ip):
    code, text = run([protonvpn, "status"], PROTON_TIMEOUT_S)
    if code != 0:
        log(f"status check failed: {text.strip()}")
        return False
    status = parse_status(text)
    if not status or not status[0]:
        if has_iface("/sys/class/net/proton0"):
            log("VPN status is disconnected but proton0 interface is up; skipping rotation")
        else:
            log("VPN is disconnected; skipping rotation")
        return False
    code, text = run([protonvpn, "countries", "list"], PROTON_TIMEOUT_S)
    countries = parse_countries(text) if code == 0 else []
    if code != 0 or not countries:
        log(f"country list unavailable: {text.strip()}")
        return False
    if countries_allowlist:
        allow = set(countries_allowlist)
        countries = [c for c in countries if c in allow]
    if not countries:
        log("no countries match allowlist")
        return False
    current = re.split(r"[#-]", status[0])[0] if status and status[0] else None
    failed_target = (record or {}).get("failedTarget")
    target = next_country(countries, current, failed_target)
    if not target or target == current:
        log(f"next country matches current ({current}); skipping rotation")
        return False
    attempt = int(now() * 1000)
    rotations = recent_rotations(record, now()) + [attempt]
    write_shared(state, {
        **(record or {}),
        "attemptAt": attempt,
        "rotations": rotations,
        "failedTarget": target,
    })
    code, text = run([protonvpn, "connect", "--country", target], CONNECT_TIMEOUT_S)
    connected = CONNECTED_RE.search(text)
    if code != 0 or not connected:
        log(f"connect to {target} failed: {text.strip()}")
        if status and status[0]:
            log(f"restoring connection to {status[0]}")
            rcode, rtext = run([protonvpn, "connect", status[0]], CONNECT_TIMEOUT_S)
            rconnected = CONNECTED_RE.search(rtext)
            if rcode != 0 or not rconnected:
                log(f"restore to {status[0]} failed: {rtext.strip()}")
            else:
                log(f"restored connection to {status[0]}")
        return False
    at = int(now() * 1000)
    address = IP_RE.search(text)
    entry_ip = address.group(1) if address else None
    real_ip = get_ip() if get_ip else None
    write_shared(state, {
        "at": at,
        "attemptAt": attempt,
        "rotations": rotations,
        "country": target,
        "server": connected.group(1) if connected else None,
        "location": connected.group(2) if connected else None,
        "egressIp": real_ip or entry_ip,
    })
    return True


def main(argv=None, env=os.environ, stdin=sys.stdin, run=run, now=time.time, sleep=time.sleep, has_iface=os.path.exists, get_ip=fetch_egress_ip):
    try:
        payload = json.load(stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    base = Path(env.get("XDG_RUNTIME_DIR") or "/tmp")
    lock = base / "pi-vpn-rotate.lock"
    state = base / "pi-vpn-rotate.json"
    log = Log(base / "pi-vpn-rotate.log", now)
    protonvpn = env.get("PI_VPN_ROTATE_PROTONVPN", "/run/current-system/sw/bin/protonvpn")
    cooldown_s = int(env.get("PI_VPN_ROTATE_COOLDOWN_S", "600"))
    max_per_hour = int(env.get("PI_VPN_ROTATE_MAX_PER_HOUR", "3"))
    countries_env = env.get("PI_VPN_ROTATE_COUNTRIES", "")
    countries_allowlist = [c.strip().upper() for c in countries_env.split(",") if c.strip()]
    what = f"{payload.get('provider', '?')} {payload.get('errorKind', '?')}"
    failed_at = now()
    lock_file = acquire(lock, now, sleep)
    if not lock_file:
        log(f"{what}: another session holds the rotation lock")
        return 1
    try:
        record = read_shared(state)
        verdict = decide(record, failed_at, payload.get("errorMessage", ""), cooldown_s, max_per_hour)
        if verdict is not None:
            code, reason = verdict
            log(f"{what}: {reason}, exit {code}")
            return code
        rotated = rotate(state, record, run, now, protonvpn, log, countries_allowlist, has_iface, get_ip=get_ip)
        log(f"{what}: {'rotated' if rotated else 'rotation failed'}")
        return 0 if rotated else 1
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
