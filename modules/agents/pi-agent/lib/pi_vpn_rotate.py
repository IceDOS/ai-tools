"""pi-agent hooks.rateLimit command: rotate the ProtonVPN server; exit 0 means retry now."""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

DEBOUNCE_S = 10
LOCK_WAIT_S = 300
PROTON_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 50
PROBE_TIMEOUT_S = 6
SETTLE_S = 2
MAX_CANDIDATES = 2
DEFAULT_FLAGGED_TTL_S = 3600
# Measured from the failure; must stay below timeoutMs of the hook in icedos.nix.
ROTATE_BUDGET_S = 290
CANDIDATE_COST_S = 2 * CONNECT_TIMEOUT_S + SETTLE_S + PROBE_TIMEOUT_S
DEFAULT_PROBE_MODEL = "big-pickle"
FREE_PLAN_TEXT = "is not available on the free plan"
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

PROBE_SCRIPT = """
const crypto = require("node:crypto");
const BASE_URL = process.env.PI_OPENCODE_FREE_BASE_URL ?? "https://opencode.ai/inference/openai/v1";
const USER_AGENT = process.env.PI_OPENCODE_FREE_UA ?? "opencode/latest/2.2.6/cli";
const ID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
let lastTimestamp = 0, counter = 0;
function descendingId(timestamp = Date.now()) {
  if (timestamp !== lastTimestamp) { lastTimestamp = timestamp; counter = 0; }
  counter++;
  const current = BigInt(timestamp) * 0x1000n + BigInt(counter);
  const value = ~current;
  const time = Array.from({ length: 6 }, (_, i) => Number((value >> BigInt(40 - 8 * i)) & 0xffn).toString(16).padStart(2, "0")).join("");
  const bytes = crypto.getRandomValues(new Uint8Array(14));
  return "ses_" + time + Array.from(bytes, (b) => ID_CHARS[b % 62]).join("");
}
async function probe() {
  const sesId = descendingId();
  const cwd = process.cwd();
  const projectHash = crypto.createHash("sha1").update("directory:" + cwd).digest("hex");
  const harness = "You are an AI agent running in OpenCode, a coding agent harness. Help the user accomplish their goals using the tools you have available.\\n\\n<env>\\n  Working directory: " + cwd + "\\n  Workspace root folder: " + cwd + "\\n  Is directory a git repo: yes\\n  Platform: linux\\n</env>";
  try {
    const res = await fetch(BASE_URL + "/chat/completions", {
      method: "POST",
      headers: {
        "authorization": "Bearer public",
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "x-opencode-client": "cli",
        "x-opencode-session": sesId,
        "x-opencode-session-id": sesId,
        "x-session-affinity": sesId,
        "x-session-id": sesId,
        "x-opencode-project": projectHash,
      },
      body: JSON.stringify({
        model: __PROBE_MODEL__,
        messages: [{ role: "system", content: harness }, { role: "user", content: "ping" }],
        stream: true,
        max_completion_tokens: 1,
        tools: [{ type: "function", function: { name: "read", description: "placeholder", parameters: { type: "object", properties: { filePath: { type: "string" } }, required: ["filePath"] } } }],
        tool_choice: "none"
      }),
      signal: AbortSignal.timeout(5000),
    });
    process.stdout.write(String(res.status));
    if (res.body) {
      try { await res.body.cancel(); } catch {}
    }
  } catch (err) {
    process.stdout.write("0");
  }
}
probe();
"""


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


def capture(argv, timeout):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error), ""
    return out.returncode, out.stdout, out.stderr


def run(argv, timeout):
    code, stdout, stderr = capture(argv, timeout)
    return code, f"{stdout}\n{stderr}"


def run_stdout(argv, timeout):
    code, stdout, _ = capture(argv, timeout)
    return code, stdout


def parse_countries(text):
    codes = []
    for line in text.splitlines():
        match = COUNTRY_RE.match(line)
        if match:
            codes.append(match.group(2))
    return codes


def base_country(server):
    return re.split(r"[#-]", server)[0]


def parse_status(text):
    if not re.search(r"^Status:\s*Connected", text, re.M):
        return None
    match = SERVER_RE.search(text)
    return (match.group(1), match.group(2)) if match else ("", "")


def entry_ttl_left(entry, now_s):
    if not isinstance(entry, dict):
        return 0
    return (entry.get("at", 0) + entry.get("ttl", DEFAULT_FLAGGED_TTL_S)) - now_s


def clean_flagged(flagged, now_s):
    return {k: v for k, v in (flagged or {}).items() if entry_ttl_left(v, now_s) > 0}


def is_flagged(key, flagged, now_s):
    if not key:
        return False
    return entry_ttl_left((flagged or {}).get(key), now_s) > 0


def time_left(key, flagged, now_s):
    if not key:
        return 0
    return max(0, int(entry_ttl_left((flagged or {}).get(key), now_s)))


def next_country(countries, current, failed_target=None):
    if not countries:
        return None
    start = failed_target if (failed_target and failed_target in countries and failed_target != current) else current
    start_idx = countries.index(start) if start in countries else -1
    for offset in range(1, len(countries) + 1):
        cand = countries[(start_idx + offset) % len(countries)]
        if cand != current:
            return cand
    return None


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


def decide(record, failed_at, message, cooldown_s, max_per_hour, state=None, session="", retry=0, max_retries=3):
    """None means rotate; otherwise (exit code, reason)."""
    if USAGE_LIMIT_RE.search(message or ""):
        return 1, "account limit"
    if not record:
        return None
    last_success = record.get("at", 0) / 1000
    # Sessions share one tunnel and usually hit the same limit together; each adopts a rotation once.
    if retry == 0 and last_success >= failed_at - DEBOUNCE_S:
        debounced = record.get("debouncedSessions", [])
        if session not in debounced:
            if state:
                write_shared(state, {**record, "debouncedSessions": debounced + [session]})
            return 0, "debounced"
    last_attempt = record.get("attemptAt", record.get("at", 0)) / 1000
    if failed_at - last_attempt < cooldown_s:
        if retry <= 0 or retry >= max_retries:
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


def probe_opencode(bun_bin=None, model=DEFAULT_PROBE_MODEL, timeout=PROBE_TIMEOUT_S):
    bun = bun_bin or os.environ.get("PI_VPN_ROTATE_BUN")
    if not bun:
        bun = shutil.which("bun")
    if not bun:
        return "missing_bun"
    code, out = run_stdout([bun, "-e", PROBE_SCRIPT.replace("__PROBE_MODEL__", json.dumps(model))], timeout)
    status = out.strip()
    if code != 0 or status in ("", "0"):
        return "timeout"
    return status


def restore_on_signal(pending, lock_file, log):
    def handler(signum, _frame):
        argv = pending.get("restore")
        if argv:
            log(f"signal {signum} during rotation; restoring in the background")
            try:
                # A new session survives the hook's SIGKILL; the inherited fd holds the lock until the restore ends.
                subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(lock_file.fileno(),),
                    preexec_fn=lambda: signal.alarm(CONNECT_TIMEOUT_S),
                )
            except OSError as error:
                log(f"background restore failed: {error}")
        os._exit(128 + signum)

    return {signum: signal.signal(signum, handler) for signum in (signal.SIGTERM, signal.SIGINT)}


def rotate(state, record, run, now, protonvpn, log, countries_allowlist=None, has_iface=os.path.exists, get_ip=fetch_egress_ip, probe=None, max_attempts=MAX_CANDIDATES, *, sleep, deadline, pending, flag_current=False):
    code, text = run([protonvpn, "status"], PROTON_TIMEOUT_S)
    if code != 0:
        log(f"status check failed: {text.strip()}")
        return False
    status = parse_status(text)
    if not status or not status[0]:
        if has_iface("/sys/class/net/proton0"):
            log("VPN status is disconnected but proton0 interface is up; skipping rotation")
            return False
        log("VPN is disconnected; connecting to new server")
        is_initial_connect = True
        status = ("", "")
        current = None
    else:
        is_initial_connect = False
        current = base_country(status[0])
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
    pool = [c for c in countries if c != current] if current else countries
    if not pool:
        log(f"next country matches current ({current}); skipping rotation")
        return False
    failed_target = (record or {}).get("failedTarget")
    attempts_limit = min(len(pool), max_attempts) if probe else 1
    tried = set()
    free_plan = False
    restore_argv = [protonvpn, "disconnect"] if is_initial_connect else [protonvpn, "connect", status[0]]
    restore_label = "disconnected" if is_initial_connect else status[0]
    flagged = clean_flagged((record or {}).get("flagged"), now())
    if flag_current and status[0]:
        flagged[status[0]] = {"at": now(), "ttl": DEFAULT_FLAGGED_TTL_S, "reason": "rate_limit"}

    while len(tried) < attempts_limit:
        if now() + CANDIDATE_COST_S + CONNECT_TIMEOUT_S > deadline:
            log("rotation time budget spent; stopping rotation")
            break
        target = next_country(countries, current, failed_target)
        if target is None or target in tried:
            break
        tried.add(target)
        failed_target = target
        attempt = int(now() * 1000)
        state_write = {**(record or {}), "attemptAt": attempt, "failedTarget": target}
        if flagged:
            state_write["flagged"] = flagged
        write_shared(state, state_write)
        pending["restore"] = restore_argv
        cmd = [protonvpn, "connect"] if free_plan else [protonvpn, "connect", "--country", target, "--random"]
        code, text = run(cmd, CONNECT_TIMEOUT_S)
        if not free_plan and code != 0 and FREE_PLAN_TEXT in text:
            # The free plan rejects --country, --random and server names; only a plain connect works.
            log("free plan cannot pick a location; using plain connect")
            free_plan = True
            restore_argv = [protonvpn, "disconnect"] if is_initial_connect else [protonvpn, "connect"]
            restore_label = "disconnected" if is_initial_connect else "a free server"
            pending["restore"] = restore_argv
            code, text = run([protonvpn, "connect"], CONNECT_TIMEOUT_S)
        connected = CONNECTED_RE.search(text)
        if code != 0 or not connected:
            log(f"connect to {target} failed: {text.strip()}")
            if not probe:
                break
            continue
        server_tag = connected.group(1)
        if free_plan and status[0] and server_tag == status[0]:
            log(f"free plan reconnected to the same server ({server_tag}); skipping rotation")
            pending.clear()
            return False
        if is_flagged(server_tag, flagged, now()):
            left = time_left(server_tag, flagged, now())
            log(f"target {target} ({server_tag}) is known rate-limited; trying next country (refresh in {left}s)")
            continue
        if probe:
            sleep(SETTLE_S)
            res = probe()
            if res is True or res == "200":
                pass
            elif res == "missing_bun":
                log("bun not found; skipping probe")
            else:
                probe_status = str(res)
                if probe_status == "429" or res is False:
                    flagged[server_tag] = {"at": now(), "ttl": DEFAULT_FLAGGED_TTL_S, "reason": "rate_limit"}
                    state_write = {**(record or {}), "flagged": flagged, "attemptAt": attempt, "failedTarget": target}
                    write_shared(state, state_write)
                    left = time_left(server_tag, flagged, now())
                    log(f"target {target} ({server_tag}) rate-limited on provider; trying next country (refresh in {left}s)")
                    continue
                if probe_status in ("0", "timeout"):
                    log(f"target {target} ({server_tag}) probe timed out; trying next country")
                    continue
                if probe_status in ("401", "403"):
                    log(f"target {target} ({server_tag}) probe failed with {probe_status} (auth error); stopping rotation")
                    break
                if probe_status.startswith("5"):
                    log(f"target {target} ({server_tag}) probe failed with {probe_status} (provider error); stopping rotation")
                    break
                log(f"target {target} ({server_tag}) probe returned {probe_status}, which says nothing about this IP; keeping server")
        flagged.pop(server_tag, None)
        pending.clear()
        at = int(now() * 1000)
        address = IP_RE.search(text)
        entry_ip = address.group(1) if address else None
        real_ip = get_ip() if get_ip else None
        state_data = {
            "at": at,
            "attemptAt": attempt,
            "rotations": recent_rotations(record, now()) + [at],
            "country": base_country(server_tag) if free_plan else target,
            "server": server_tag,
            "location": connected.group(2),
            "egressIp": real_ip or entry_ip,
        }
        if flagged:
            state_data["flagged"] = flagged
        write_shared(state, state_data)
        return True

    if tried:
        if is_initial_connect:
            log("all connection attempts failed; disconnecting")
            run([protonvpn, "disconnect"], CONNECT_TIMEOUT_S)
        elif is_flagged(status[0], flagged, now()):
            log(f"original server {status[0]} is rate-limited; keeping current connection to {restore_label}")
        else:
            log(f"restoring connection to {restore_label}")
            rcode, rtext = run(restore_argv, CONNECT_TIMEOUT_S)
            if rcode != 0 or not CONNECTED_RE.search(rtext):
                log(f"restore to {restore_label} failed: {rtext.strip()}")
            else:
                log(f"restored connection to {restore_label}")
    pending.clear()
    return False


def main(argv=None, env=os.environ, stdin=sys.stdin, run=run, now=time.time, sleep=time.sleep, has_iface=os.path.exists, get_ip=fetch_egress_ip, probe=None):
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
    try:
        max_retries = int(env.get("PI_VPN_ROTATE_MAX_RETRIES", "3"))
    except (ValueError, TypeError):
        max_retries = 3
    countries_env = env.get("PI_VPN_ROTATE_COUNTRIES", "")
    countries_allowlist = [c.strip().upper() for c in countries_env.split(",") if c.strip()]
    what = f"{payload.get('provider', '?')} {payload.get('errorKind', '?')}"
    failed_at = now()
    lock_file = acquire(lock, now, sleep)
    if not lock_file:
        log(f"{what}: another session holds the rotation lock")
        return 1
    previous_handlers = {}
    try:
        record = read_shared(state)
        pending = {}
        previous_handlers = restore_on_signal(pending, lock_file, log)
        retry = payload.get("retry", 0)
        verdict = decide(record, failed_at, payload.get("errorMessage", ""), cooldown_s, max_per_hour, state=state, session=payload.get("session_id", ""), retry=retry, max_retries=max_retries)
        if verdict is not None:
            code, reason = verdict
            log(f"{what}: {reason}, exit {code}")
            return code
        probe_fn = probe
        if probe_fn is None and payload.get("provider") in ("opencode-free", "opencode") and env.get("PI_VPN_ROTATE_PROBE", "1") != "0":
            bun_path = env.get("PI_VPN_ROTATE_BUN") or shutil.which("bun")
            if not bun_path:
                log("bun not found; probe disabled")
            else:
                # The public token only reaches free models, so paid opencode models keep the default.
                model = (payload.get("provider") == "opencode-free" and payload.get("model")) or DEFAULT_PROBE_MODEL

                def opencode_probe():
                    return probe_opencode(bun_bin=bun_path, model=model)

                probe_fn = opencode_probe
        try:
            max_candidates = int(env.get("PI_VPN_ROTATE_MAX_CANDIDATES") or MAX_CANDIDATES)
        except (ValueError, TypeError):
            max_candidates = MAX_CANDIDATES
        flag_current = payload.get("errorKind") in ("rate_limit", "usage_limit")
        rotated = rotate(state, record, run, now, protonvpn, log, countries_allowlist, has_iface, get_ip=get_ip, probe=probe_fn, max_attempts=max_candidates, sleep=sleep, deadline=failed_at + ROTATE_BUDGET_S, pending=pending, flag_current=flag_current)
        log(f"{what}: {'rotated' if rotated else 'rotation failed'}")
        return 0 if rotated else 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
