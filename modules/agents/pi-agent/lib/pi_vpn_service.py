"""pi-agent VPN rotation service: daemon managing ProtonVPN connections and rate-limit state."""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DEBOUNCE_S = int(os.environ.get("PI_VPN_ROTATE_DEBOUNCE_S", "60"))
PROTON_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 50
PROBE_TIMEOUT_S = 6
SETTLE_S = 2
DEFAULT_FLAGGED_TTL_S = 3600
DEFAULT_UNREACHABLE_TTL_S = 300
HOUR_S = 3600
ROTATE_BUDGET_S = 290
CANDIDATE_COST_S = CONNECT_TIMEOUT_S + SETTLE_S + PROBE_TIMEOUT_S
MAX_ATTEMPTS_PER_COUNTRY = 3
LOG_LINES = 200
FREE_PLAN_TEXT = "is not available on the free plan"

COUNTRY_RE = re.compile(r"^(.+?)\s+([A-Z]{2})\s*$")
SERVER_RE = re.compile(r"^Server:\s*(\S+)\s+in\s+(.+?)\s*$", re.M)
CONNECTED_RE = re.compile(r"Connected to (\S+) in (.+?)\.\s*$", re.M)
IP_RE = re.compile(r"Your new IP address is ([0-9.]+)\.", re.M)
USAGE_LIMIT_RE = re.compile(
    r"GoUsageLimitError|Monthly usage limit|available balance"
    r"|insufficient_quota|out of budget|quota exceeded|billing",
    re.I,
)

PROBE_SCRIPT = """
const crypto = require("node:crypto");
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
  // URL, user-agent and model come from the extensions' free-models.ts; without it, skip the probe.
  let mod;
  try { mod = await import(process.env.PI_OPENCODE_FREE_MODULE); } catch { process.stdout.write("skip"); return; }
  const { BASE_URL, ZEN_USER_AGENT: USER_AGENT } = mod;
  const model = __PROBE_MODEL__ ?? mod.loadCatalog().models[0]?.id;
  if (!model) { process.stdout.write("skip"); return; }
  const sesId = descendingId();
  const cwd = process.cwd();
  const projectHash = crypto.createHash("sha1").update("directory:" + cwd).digest("hex");
  const harness = `You are an AI agent running in OpenCode, a coding agent harness. Help the user accomplish their goals using the tools you have available.

<env>
  Working directory: ${cwd}
  Workspace root folder: ${cwd}
  Is directory a git repo: yes
  Platform: linux
</env>`;
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
        model,
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
    def __init__(self, path, now_fn=time.time):
        self.path = Path(path) if path else None
        self.now_fn = now_fn

    def __call__(self, text):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.now_fn()))
        msg = f"pi-vpn-service: {text}"
        print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                old = self.path.read_text().splitlines() if self.path.exists() else []
                self.path.write_text("\n".join((old + [f"{stamp} {text}"])[-LOG_LINES:]) + "\n")
            except OSError:
                pass


def capture(argv, timeout):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout + out.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as err:
        return 1, str(err)


def run_cmd(argv, timeout):
    return capture(argv, timeout)


def probe_opencode(bun_bin="bun", model=None):
    if not shutil.which(bun_bin) and not os.path.exists(bun_bin):
        return "missing_bun"
    script = PROBE_SCRIPT.replace("__PROBE_MODEL__", json.dumps(model))
    code, out = capture([bun_bin, "-e", script], timeout=PROBE_TIMEOUT_S + 2)
    return out.strip() if code == 0 else "0"


def fetch_egress_ip(bun_bin="bun"):
    bun = shutil.which(bun_bin) or (bun_bin if os.path.exists(bun_bin) else None)
    if bun:
        code, out = capture(
            [bun, "-e", "fetch('https://api.ipify.org').then(r=>r.text()).then(t=>process.stdout.write(t.trim())).catch(()=>{})"],
            timeout=5,
        )
        if code == 0 and IP_RE.match(f"Your new IP address is {out.strip()}."):
            return out.strip()
    return None


def parse_status(text):
    match = SERVER_RE.search(text)
    return (match.group(1), match.group(2)) if match else None


def parse_countries(text):
    res = []
    for line in text.splitlines():
        match = COUNTRY_RE.match(line.strip())
        if match:
            res.append(match.group(2))
    return res


def base_country(server):
    if not server:
        return ""
    code = server.split("-", 1)[0].split("#", 1)[0].upper()
    return "".join(c for c in code if c.isalpha())


def next_country(pool, current, failed=None):
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    start_idx = 0
    if current and current in pool:
        start_idx = (pool.index(current) + 1) % len(pool)
    elif failed and failed in pool:
        start_idx = (pool.index(failed) + 1) % len(pool)
    for offset in range(len(pool)):
        cand = pool[(start_idx + offset) % len(pool)]
        if cand != current:
            return cand
    return pool[0]


class VpnState:
    def __init__(self, state_file, runtime_file, now_fn=time.time):
        self.state_file = Path(state_file)
        self.runtime_file = Path(runtime_file)
        self.now_fn = now_fn
        self.lock = threading.Lock()
        self.rate_limited_countries = {}
        self.rate_limited_servers = {}
        self.rate_limited_ips = {}
        self.current_server = None
        self.current_country = None
        self.current_location = None
        self.current_ip = None
        self.last_rotation_time = 0
        self.last_attempt_at = 0
        self.rotations = []
        self.debounced_sessions = []
        self.all_rate_limited = False
        self.min_refresh_in = 0
        self.failed_target = None
        self.is_rotating = False
        self.load()

    def clean_expired(self):
        now = self.now_fn()
        self.rate_limited_countries = {
            k: v for k, v in self.rate_limited_countries.items()
            if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now
        }
        self.rate_limited_servers = {
            k: v for k, v in self.rate_limited_servers.items()
            if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now
        }
        self.rate_limited_ips = {
            k: v for k, v in self.rate_limited_ips.items()
            if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now
        }
        cutoff = now - HOUR_S
        self.rotations = [t for t in self.rotations if t >= cutoff]

    def load(self):
        with self.lock:
            self.clean_expired()
            if not self.state_file.exists():
                return
            try:
                data = json.loads(self.state_file.read_text())
                now = self.now_fn()
                for k, v in data.get("rateLimitedCountries", {}).items():
                    if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now:
                        self.rate_limited_countries[k] = v
                for k, v in data.get("rateLimitedServers", {}).items():
                    if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now:
                        self.rate_limited_servers[k] = v
                for k, v in data.get("rateLimitedIps", {}).items():
                    if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now:
                        self.rate_limited_ips[k] = v
                self.current_server = data.get("currentServer") or data.get("server")
                self.current_country = data.get("currentCountry") or data.get("country")
                self.current_location = data.get("currentLocation") or data.get("location")
                self.current_ip = data.get("currentIp") or data.get("egressIp")
                self.last_rotation_time = data.get("lastRotationTime", 0)
                self.last_attempt_at = data.get("lastAttemptAt", 0)
                self.rotations = [t for t in data.get("rotations", []) if t >= now - HOUR_S]
                self.failed_target = data.get("failedTarget")
            except Exception:
                pass

    def save(self):
        with self.lock:
            self.clean_expired()
            now = self.now_fn()
            p_data = {
                "rateLimitedCountries": self.rate_limited_countries,
                "rateLimitedServers": self.rate_limited_servers,
                "rateLimitedIps": self.rate_limited_ips,
                "currentServer": self.current_server,
                "currentCountry": self.current_country,
                "currentLocation": self.current_location,
                "currentIp": self.current_ip,
                "lastRotationTime": self.last_rotation_time,
                "lastAttemptAt": self.last_attempt_at,
                "rotations": self.rotations,
                "failedTarget": self.failed_target,
                "at": int(now * 1000),
            }
            ident = threading.get_ident()
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_p = self.state_file.with_name(f"{self.state_file.name}.{os.getpid()}.{ident}.tmp")
                tmp_p.write_text(json.dumps(p_data, indent=2))
                tmp_p.replace(self.state_file)
            except OSError:
                pass

            r_countries = self.get_rate_limited_countries_list()
            r_data = {
                "at": int(now * 1000),
                "server": self.current_server,
                "country": self.current_country,
                "location": self.current_location,
                "egressIp": self.current_ip,
                "allRateLimited": self.all_rate_limited,
                "refreshIn": self.min_refresh_in,
                "rateLimitedCountries": r_countries,
                "rateLimitedCountriesCount": len(r_countries),
                "rotating": self.is_rotating,
                "rotatedAt": self.last_rotation_time,
            }
            try:
                self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_r = self.runtime_file.with_name(f"{self.runtime_file.name}.{os.getpid()}.{ident}.tmp")
                tmp_r.write_text(json.dumps(r_data))
                tmp_r.replace(self.runtime_file)
            except OSError:
                pass

    def flag_country(self, country, ttl=DEFAULT_FLAGGED_TTL_S, reason="rate_limit"):
        if not country:
            return
        self.rate_limited_countries[country] = {
            "at": self.now_fn(),
            "ttl": ttl,
            "reason": reason,
        }
        self.save()

    def flag_server(self, server, ip=None, country=None, ttl=DEFAULT_FLAGGED_TTL_S, reason="rate_limit"):
        if not server and not ip:
            return
        now = self.now_fn()
        if server:
            self.rate_limited_servers[server] = {
                "at": now,
                "ttl": ttl,
                "ip": ip,
                "country": country or base_country(server),
                "reason": reason,
            }
        if ip:
            self.rate_limited_ips[ip] = {
                "at": now,
                "ttl": ttl,
                "server": server,
                "country": country or base_country(server),
                "reason": reason,
            }
        self.save()

    def is_country_rate_limited(self, country):
        self.clean_expired()
        return country in self.rate_limited_countries

    def is_server_rate_limited(self, server, ip=None):
        self.clean_expired()
        if server and server in self.rate_limited_servers:
            return True
        if ip and ip in self.rate_limited_ips:
            return True
        return False

    def get_rate_limited_countries_list(self):
        self.clean_expired()
        now = self.now_fn()
        res = []
        for code, info in self.rate_limited_countries.items():
            left = max(0, int((info.get("at", 0) + info.get("ttl", DEFAULT_FLAGGED_TTL_S)) - now))
            res.append({"country": code, "refreshIn": left, "reason": info.get("reason", "rate_limit")})
        res.sort(key=lambda x: x["refreshIn"])
        return res

    def get_min_refresh_in(self, pool=None):
        self.clean_expired()
        now = self.now_fn()
        times = []
        check_countries = pool if pool is not None else list(self.rate_limited_countries.keys())
        for c in check_countries:
            info = self.rate_limited_countries.get(c)
            if info:
                left = (info.get("at", 0) + info.get("ttl", DEFAULT_FLAGGED_TTL_S)) - now
                if left > 0:
                    times.append(int(left))
        for info in self.rate_limited_servers.values():
            left = (info.get("at", 0) + info.get("ttl", DEFAULT_FLAGGED_TTL_S)) - now
            if left > 0:
                times.append(int(left))
        for info in self.rate_limited_ips.values():
            left = (info.get("at", 0) + info.get("ttl", DEFAULT_FLAGGED_TTL_S)) - now
            if left > 0:
                times.append(int(left))
        return min(times) if times else 0

    def clear(self):
        with self.lock:
            self.rate_limited_countries.clear()
            self.rate_limited_servers.clear()
            self.rate_limited_ips.clear()
            self.rotations.clear()
            self.debounced_sessions.clear()
            self.all_rate_limited = False
            self.min_refresh_in = 0
            self.failed_target = None
        self.save()


class VpnRotator:
    def __init__(
        self,
        state,
        log,
        protonvpn="protonvpn",
        countries_allowlist=None,
        countries_blocklist=None,
        cooldown_s=600,
        max_per_hour=3,
        rotate_budget_s=ROTATE_BUDGET_S,
        run_fn=run_cmd,
        now_fn=time.time,
        sleep_fn=time.sleep,
        has_iface=os.path.exists,
        get_ip=fetch_egress_ip,
        probe=None,
        bun_bin=None,
    ):
        self.state = state
        self.log = log
        self.protonvpn = protonvpn
        self.countries_allowlist = [c.upper() for c in countries_allowlist] if countries_allowlist else []
        self.countries_blocklist = [c.upper() for c in countries_blocklist] if countries_blocklist else []
        self.cooldown_s = cooldown_s
        self.max_per_hour = max_per_hour
        self.rotate_budget_s = rotate_budget_s
        self.run = run_fn
        self.now = now_fn
        self.sleep = sleep_fn
        self.has_iface = has_iface
        self.get_ip = get_ip
        self.probe = probe
        self.bun_bin = bun_bin

    def get_live_status(self):
        code, text = self.run([self.protonvpn, "status"], PROTON_TIMEOUT_S)
        if code != 0:
            return {"connected": False, "server": None, "country": None, "location": None, "egressIp": None}
        status = parse_status(text)
        if not status or not status[0]:
            return {"connected": False, "server": None, "country": None, "location": None, "egressIp": None}
        server = status[0]
        country = base_country(server)
        location = status[1]
        return {
            "connected": True,
            "server": server,
            "country": country,
            "location": location,
            "egressIp": self.state.current_ip,
        }

    def rotate(self, req):
        err_msg = req.get("errorMessage", "")
        if USAGE_LIMIT_RE.search(err_msg):
            self.log("account limit encountered; skipping rotation")
            return {"ok": False, "reason": "account limit"}

        now = self.now()
        retry = req.get("retry", 0)

        # Enforce hourly rotation cap
        cutoff = now - HOUR_S
        recent = [t for t in self.state.rotations if t >= cutoff]
        if self.max_per_hour > 0 and len(recent) >= self.max_per_hour:
            self.log(f"hourly rotation cap reached ({len(recent)}/{self.max_per_hour})")
            return {"ok": False, "reason": "hourly cap"}

        # Enforce cooldown on initial failure (retries within a prompt proceed)
        if retry == 0 and self.state.last_attempt_at:
            elapsed = now - self.state.last_attempt_at
            if elapsed < self.cooldown_s:
                self.log(f"cooldown in effect ({int(self.cooldown_s - elapsed)}s remaining)")
                return {"ok": False, "reason": "cool-down"}

        deadline = now + self.rotate_budget_s
        flag_current = req.get("errorKind") in ("rate_limit", "usage_limit")

        code, text = self.run([self.protonvpn, "status"], PROTON_TIMEOUT_S)
        if code != 0:
            self.log(f"status check failed: {text.strip()}")
            return {"ok": False, "reason": f"status check failed: {text.strip()}"}

        status = parse_status(text)
        current_server = None
        current_country = None

        if not status or not status[0]:
            if self.has_iface("/sys/class/net/proton0"):
                self.log("VPN status is disconnected but proton0 interface is up; skipping rotation")
                return {"ok": False, "reason": "proton0 up but disconnected"}
            self.log("VPN is disconnected; connecting to new server")
        else:
            current_server = status[0]
            current_country = base_country(current_server)
            self.state.current_server = current_server
            self.state.current_country = current_country
            self.state.current_location = status[1]

        if flag_current and current_server:
            self.log(f"flagging current server {current_server} (country {current_country}) as rate-limited")
            self.state.flag_server(current_server, country=current_country)

        code, text = self.run([self.protonvpn, "countries", "list"], PROTON_TIMEOUT_S)
        if code != 0:
            self.log(f"country list unavailable: {text.strip()}")
            return {"ok": False, "reason": "country list unavailable"}
        all_countries = parse_countries(text)
        if not all_countries:
            self.log("no countries returned by protonvpn")
            return {"ok": False, "reason": "no countries returned by protonvpn"}

        if self.countries_allowlist:
            allow = set(self.countries_allowlist)
            pool = [c for c in all_countries if c in allow]
        else:
            pool = list(all_countries)

        if self.countries_blocklist:
            block = set(self.countries_blocklist)
            pool = [c for c in pool if c not in block]

        if not pool:
            self.log("no countries left after allowlist and blocklist")
            return {"ok": False, "reason": "no countries left after allowlist and blocklist"}

        candidates = [c for c in pool if not self.state.is_country_rate_limited(c)]
        single_country_mode = len(pool) == 1

        if not candidates and not single_country_mode:
            min_refresh = self.state.get_min_refresh_in(pool)
            self.state.all_rate_limited = True
            self.state.min_refresh_in = min_refresh
            self.state.save()
            self.log(f"all available countries ({len(pool)}) are rate-limited; next refresh in {min_refresh}s")
            return {
                "ok": False,
                "reason": "all_rate_limited",
                "refreshIn": min_refresh,
                "rateLimitedCountries": self.state.get_rate_limited_countries_list(),
            }

        probe_fn = self.probe
        if probe_fn is None and req.get("provider") in ("opencode-free", "opencode"):
            bun_path = self.bun_bin or os.environ.get("PI_VPN_ROTATE_BUN") or shutil.which("bun")
            if bun_path:
                # None lets the probe pick a model the cached catalog still serves.
                model = (req.get("provider") == "opencode-free" and req.get("model")) or None

                def opencode_probe():
                    return probe_opencode(bun_bin=bun_path, model=model)

                probe_fn = opencode_probe

        free_plan = False
        tried_countries = set()
        country_attempts = {}
        country_failure_reason = {}
        failed_target = self.state.failed_target
        success_info = None

        while True:
            if self.now() + CANDIDATE_COST_S > deadline:
                self.log("rotation time budget exhausted")
                break

            active_pool = [c for c in pool if not self.state.is_country_rate_limited(c)]
            if not active_pool:
                break

            target = next_country(active_pool, current_country, failed_target)
            if not target or (target in tried_countries and not single_country_mode):
                unflagged = [c for c in active_pool if c not in tried_countries]
                if unflagged:
                    target = unflagged[0]
                else:
                    break

            attempts = country_attempts.get(target, 0)
            if attempts >= MAX_ATTEMPTS_PER_COUNTRY:
                tried_countries.add(target)
                reason = country_failure_reason.get(target, "rate_limit")
                ttl = DEFAULT_FLAGGED_TTL_S if reason == "rate_limit" else DEFAULT_UNREACHABLE_TTL_S
                self.state.flag_country(target, ttl=ttl, reason=reason)
                continue

            country_attempts[target] = attempts + 1
            failed_target = target
            self.state.failed_target = target

            cmd = [self.protonvpn, "connect"] if free_plan else [self.protonvpn, "connect", "--country", target, "--random"]
            code, text = self.run(cmd, CONNECT_TIMEOUT_S)

            if not free_plan and code != 0 and FREE_PLAN_TEXT in text:
                self.log("free plan cannot pick a location; using plain connect")
                free_plan = True
                code, text = self.run([self.protonvpn, "connect"], CONNECT_TIMEOUT_S)

            connected = CONNECTED_RE.search(text)
            if code != 0 or not connected:
                self.log(f"connect to {target} failed: {text.strip()}")
                country_failure_reason.setdefault(target, "unreachable")
                if country_attempts[target] >= MAX_ATTEMPTS_PER_COUNTRY:
                    tried_countries.add(target)
                    self.state.flag_country(target, ttl=DEFAULT_UNREACHABLE_TTL_S, reason="unreachable")
                    if single_country_mode:
                        break
                continue

            server_tag = connected.group(1)
            location = connected.group(2)
            ip_match = IP_RE.search(text)
            entry_ip = ip_match.group(1) if ip_match else None
            real_ip = self.get_ip() if self.get_ip else None
            egress_ip = real_ip or entry_ip

            if self.state.is_server_rate_limited(server_tag, egress_ip):
                self.log(f"connected server {server_tag} ({egress_ip}) is known rate-limited; trying next")
                country_failure_reason[target] = "rate_limit"
                if country_attempts[target] >= MAX_ATTEMPTS_PER_COUNTRY:
                    tried_countries.add(target)
                    self.state.flag_country(target, ttl=DEFAULT_FLAGGED_TTL_S, reason="rate_limit")
                continue

            if probe_fn:
                self.sleep(SETTLE_S)
                p_res = probe_fn()
                if p_res is True or p_res == "200":
                    pass
                elif p_res == "missing_bun":
                    self.log("bun not found; skipping probe")
                elif p_res == "skip":
                    self.log("no free-models module or cached catalog; skipping probe")
                else:
                    probe_status = str(p_res)
                    if probe_status == "429" or p_res is False:
                        self.log(f"target {target} ({server_tag}) probe returned 429; flagging server")
                        self.state.flag_server(server_tag, ip=egress_ip, country=target, reason="rate_limit")
                        country_failure_reason[target] = "rate_limit"
                        if country_attempts[target] >= MAX_ATTEMPTS_PER_COUNTRY:
                            tried_countries.add(target)
                            self.state.flag_country(target, ttl=DEFAULT_FLAGGED_TTL_S, reason="rate_limit")
                        continue
                    if probe_status in ("0", "timeout"):
                        self.log(f"target {target} ({server_tag}) probe timed out; trying next")
                        country_failure_reason.setdefault(target, "unreachable")
                        if country_attempts[target] >= MAX_ATTEMPTS_PER_COUNTRY:
                            tried_countries.add(target)
                            self.state.flag_country(target, ttl=DEFAULT_UNREACHABLE_TTL_S, reason="unreachable")
                        continue
                    if probe_status in ("401", "403"):
                        self.log(f"target {target} ({server_tag}) probe auth error {probe_status}; stopping")
                        return {"ok": False, "reason": f"auth error {probe_status}"}
                    if probe_status.startswith("5"):
                        self.log(f"target {target} ({server_tag}) probe server error {probe_status}; stopping")
                        return {"ok": False, "reason": f"server error {probe_status}"}

            success_info = {
                "server": server_tag,
                "country": base_country(server_tag) if free_plan else target,
                "location": location,
                "egressIp": egress_ip,
            }
            break

        if success_info:
            now_t = self.now()
            self.state.current_server = success_info["server"]
            self.state.current_country = success_info["country"]
            self.state.current_location = success_info["location"]
            self.state.current_ip = success_info["egressIp"]
            self.state.last_rotation_time = now_t
            self.state.last_attempt_at = now_t
            self.state.rotations.append(now_t)
            self.state.all_rate_limited = False
            self.state.min_refresh_in = 0
            self.state.failed_target = None
            self.state.save()
            self.log(f"successfully rotated to {success_info['server']} ({success_info['country']})")
            return {
                "ok": True,
                "server": success_info["server"],
                "country": success_info["country"],
                "location": success_info["location"],
                "egressIp": success_info["egressIp"],
            }

        min_refresh = self.state.get_min_refresh_in(pool)
        self.state.all_rate_limited = True
        self.state.min_refresh_in = min_refresh
        self.state.save()
        self.log(f"all available servers/countries are rate-limited; next refresh in {min_refresh}s")
        return {
            "ok": False,
            "reason": "all_rate_limited",
            "refreshIn": min_refresh,
            "rateLimitedCountries": self.state.get_rate_limited_countries_list(),
        }


class VpnService:
    def __init__(self, rotator, sock_path=None):
        self.rotator = rotator
        if sock_path:
            self.sock_path = Path(sock_path)
        else:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            self.sock_path = Path(runtime_dir) / "pi-vpn-rotate.sock"
        self.rotate_lock = asyncio.Lock()
        self.server = None

    def _try_debounce(self, session_id):
        now = self.rotator.now()
        if now - self.rotator.state.last_rotation_time < DEBOUNCE_S and self.rotator.state.current_server:
            debounced = self.rotator.state.debounced_sessions
            if session_id not in debounced:
                self.rotator.state.debounced_sessions.append(session_id)
                self.rotator.state.save()
            return {
                "ok": True,
                "debounced": True,
                "server": self.rotator.state.current_server,
                "country": self.rotator.state.current_country,
                "location": self.rotator.state.current_location,
                "egressIp": self.rotator.state.current_ip,
            }
        return None

    async def handle_rotate(self, req):
        session_id = req.get("session_id") or req.get("session", "")
        cached = self._try_debounce(session_id)
        if cached:
            return cached
        async with self.rotate_lock:
            cached = self._try_debounce(session_id)
            if cached:
                return cached
            self.rotator.state.is_rotating = True
            self.rotator.state.save()
            try:
                res = await asyncio.to_thread(self.rotator.rotate, req)
                if res.get("ok"):
                    self.rotator.state.debounced_sessions = [session_id] if session_id else []
                    self.rotator.state.save()
                return res
            finally:
                self.rotator.state.is_rotating = False
                self.rotator.state.save()

    async def handle_status(self):
        s = self.rotator.state
        live = await asyncio.to_thread(self.rotator.get_live_status)
        return {
            "ok": True,
            "connected": live.get("connected", False),
            "server": live.get("server") or s.current_server,
            "country": live.get("country") or s.current_country,
            "location": live.get("location") or s.current_location,
            "egressIp": live.get("egressIp") or s.current_ip,
            "allRateLimited": s.all_rate_limited,
            "refreshIn": s.min_refresh_in,
            "rateLimitedCountries": s.get_rate_limited_countries_list(),
            "rotating": s.is_rotating,
        }

    async def handle_clear(self):
        self.rotator.state.clear()
        return {"ok": True, "cleared": True}

    async def handle_client(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
            action = req.get("action", "rotate")
            if action == "status":
                resp = await self.handle_status()
            elif action == "clear":
                resp = await self.handle_clear()
            elif action == "rotate":
                resp = await self.handle_rotate(req)
            else:
                resp = {"ok": False, "reason": f"unknown action: {action}"}
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:
            err_resp = {"ok": False, "reason": str(e)}
            try:
                writer.write((json.dumps(err_resp) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass
        self.server = await asyncio.start_unix_server(self.handle_client, path=str(self.sock_path))

    async def stop(self):
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass


async def run_service(service, log):
    await service.start()
    log(f"service listening on {service.sock_path}")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda _s, _f: stop_event.set())

    await stop_event.wait()
    log("shutting down service")
    await service.stop()


def main():
    state_dir = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "pi-vpn-rotate"
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

    state_file = state_dir / "state.json"
    runtime_file = runtime_dir / "pi-vpn-rotate.json"
    sock_path = runtime_dir / "pi-vpn-rotate.sock"
    log_file = state_dir / "service.log"

    log = Log(log_file)
    state = VpnState(state_file, runtime_file)

    protonvpn = os.environ.get("PI_VPN_ROTATE_PROTONVPN", "protonvpn")
    cooldown_s = int(os.environ.get("PI_VPN_ROTATE_COOLDOWN_S", "600"))
    max_per_hour = int(os.environ.get("PI_VPN_ROTATE_MAX_PER_HOUR", "3"))

    raw_countries = os.environ.get("PI_VPN_ROTATE_COUNTRIES", "")
    countries = [c.strip().upper() for c in raw_countries.split(",") if c.strip()]
    raw_excluded = os.environ.get("PI_VPN_ROTATE_EXCLUDED_COUNTRIES", "")
    excluded = [c.strip().upper() for c in raw_excluded.split(",") if c.strip()]

    rotator = VpnRotator(
        state=state,
        log=log,
        protonvpn=protonvpn,
        countries_allowlist=countries,
        countries_blocklist=excluded,
        cooldown_s=cooldown_s,
        max_per_hour=max_per_hour,
    )

    service = VpnService(rotator, sock_path=sock_path)
    try:
        asyncio.run(run_service(service, log))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
