"""vpn-rotate service: rotates the opencode-free egress IP without touching the host network.

Requests run direct until the host IP is rate-limited; then only the bun child is tunnelled
through a userspace WireGuard client (wireproxy) exposing an HTTP proxy on 127.0.0.1.
"""
import asyncio
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

DEBOUNCE_S = int(os.environ.get("VPN_ROTATE_DEBOUNCE_S", "60"))
PROBE_TIMEOUT_S = 6
START_TIMEOUT_S = 20
SETTLE_S = 2
TICK_S = 30
DEFAULT_FLAGGED_TTL_S = 900
DEFAULT_UNREACHABLE_TTL_S = 300
HOUR_S = 3600
ROTATE_BUDGET_S = 290
CANDIDATE_COST_S = START_TIMEOUT_S + SETTLE_S + PROBE_TIMEOUT_S
# A server is flagged after this many failures within STRIKE_WINDOW_S, not on the first one.
STRIKES_TO_FLAG = 3
STRIKE_WINDOW_S = 900
ALL_FAILED_RETRY_S = 180
LOG_LINES = 200
CERT_REFRESH_BELOW_S = 6 * HOUR_S
CERT_RETRY_S = 600
SERVERLIST_RETRY_S = 600
SERVERLIST_TTL_S = 3 * HOUR_S
DEFAULT_PROXY_PORT = 25345
DEFAULT_DIRECT_TTL_S = 3600
DEFAULT_SERVERLIST = "~/.cache/Proton/VPN/serverlist.json"
WIREGUARD_PORT = 51820
NETSTACK_ADDRESS = "10.2.0.2/32"
NETSTACK_DNS = "10.2.0.1"
# Leaves room for an outer host VPN; at 1420 inside a 1420 tunnel packets fragment and stall.
NETSTACK_MTU = 1280
FEATURE_SECURE_CORE = 1
FEATURE_TOR = 2
STATUS_UP = 1
PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

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
  try { mod = await import(process.env.VPN_ROTATE_OPENCODE_FREE_MODULE); } catch { process.stdout.write("skip"); return; }
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
    if (res.ok) {
      try { await res.body?.cancel(); } catch {}
    } else {
      // Tab-separated detail so the service can log why the request failed.
      let body = "";
      try { body = (await res.text()).slice(0, 300); } catch {}
      process.stdout.write("\\t" + JSON.stringify({ retryAfter: res.headers.get("retry-after"), body }));
    }
  } catch (err) {
    process.stdout.write("0");
  }
}
probe();
"""


def noop(_text):
    return None


class Log:
    def __init__(self, path, now_fn=time.time):
        self.path = Path(path) if path else None
        self.now_fn = now_fn

    def __call__(self, text):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.now_fn()))
        print(f"[{stamp}] vpn-rotate-service: {text}", file=sys.stderr, flush=True)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                old = self.path.read_text().splitlines() if self.path.exists() else []
                self.path.write_text("\n".join((old + [f"{stamp} {text}"])[-LOG_LINES:]) + "\n")
            except OSError:
                pass


def capture(argv, timeout, env=None):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return out.returncode, out.stdout + out.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as err:
        return 1, str(err)


def retry_after(detail):
    """Seconds until the provider says the limit resets, from the probe's detail JSON, or None."""
    try:
        value = int(json.loads(detail).get("retryAfter"))
    except (ValueError, TypeError, AttributeError):
        return None
    return value if value > 0 else None


def probe_opencode(bun_bin="bun", model=None, env=None):
    if not shutil.which(bun_bin) and not os.path.exists(bun_bin):
        return "missing_bun"
    script = PROBE_SCRIPT.replace("__PROBE_MODEL__", json.dumps(model))
    full_env = dict(os.environ, **(env or {}))
    code, out = capture([bun_bin, "-e", script], timeout=PROBE_TIMEOUT_S + 2, env=full_env)
    return out.strip() if code == 0 else "0"


def fetch_egress_ip(bun_bin="bun", env=None):
    bun = shutil.which(bun_bin) or (bun_bin if os.path.exists(bun_bin) else None)
    if not bun:
        return None
    full_env = dict(os.environ, **(env or {}))
    code, out = capture(
        [bun, "-e", "fetch('https://api.ipify.org').then(r=>r.text()).then(t=>process.stdout.write(t.trim())).catch(()=>{})"],
        timeout=PROBE_TIMEOUT_S,
        env=full_env,
    )
    ip = out.strip()
    return ip if code == 0 and IP_RE.match(ip) else None


def flag_key(name, model=None):
    """Flags and strikes are per server (or IP) and model; a key without a model covers every model."""
    return f"{name}|{model}" if model else name


def flagged_names(flags, model=None):
    """Names whose flag applies to this model."""
    names = set()
    for key in flags:
        name, _, flag_model = key.partition("|")
        if not flag_model or flag_model == model:
            names.add(name)
    return names


def base_country(server):
    if not server:
        return ""
    code = server.split("-", 1)[0].split("#", 1)[0].upper()
    return "".join(c for c in code if c.isalpha())


class ProtonSession:
    """The signed-in Proton CLI session; the only place that imports proton.vpn."""

    def __init__(self, session_factory=None, log=None):
        self.log = log or noop
        self.session_factory = session_factory
        self._session = None

    def get(self):
        if self._session is not None:
            return self._session
        if self.session_factory:
            self._session = self.session_factory()
            return self._session
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        # Without dbus on the bus address the library silently uses a text keyring and loads no session.
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
        from proton.vpn.core.session_holder import ClientTypeMetadata, SessionHolder
        self._session = SessionHolder(ClientTypeMetadata(type="cli")).session
        return self._session

    def fetch_certificate(self):
        # Reached from tick/rotate worker threads only, so a private loop is safe here.
        # The library's _requests_unlock persists the renewed cert to the keyring.
        asyncio.run(self.get().fetch_certificate())

    def fetch_server_list(self):
        """Server list in the CLI cache schema; the library also rewrites its own cache file."""
        return asyncio.run(self.get().fetch_server_list()).to_dict()


class ServerCatalog:
    """Viable Proton servers from the CLI's cached server list, refetched when the cache is stale."""

    def __init__(self, path=DEFAULT_SERVERLIST, log=None, max_tier=None, refresh=None,
                 now_fn=time.time, retry_s=SERVERLIST_RETRY_S):
        self.path = Path(os.path.expanduser(str(path)))
        self.log = log or noop
        self.max_tier = max_tier
        self.refresh = refresh
        self.now = now_fn
        self.retry_s = retry_s
        self.entries = []
        self._mtime = None
        self._expires = None
        self._last_refresh = None

    def _entry(self, logical, max_tier):
        if logical.get("Status") != STATUS_UP:
            return None
        if int(logical.get("Tier") or 0) > max_tier:
            return None
        if int(logical.get("Features") or 0) & (FEATURE_SECURE_CORE | FEATURE_TOR):
            return None
        for srv in logical.get("Servers") or []:
            if srv.get("Status") != STATUS_UP:
                continue
            entry_ip = srv.get("EntryIP")
            public_key = srv.get("X25519PublicKey")
            if not entry_ip or not public_key:
                continue
            return {
                "server": logical.get("Name"),
                "country": logical.get("ExitCountry"),
                "city": logical.get("City"),
                "load": int(logical.get("Load") or 0),
                "entry_ip": entry_ip,
                "public_key": public_key,
            }
        return None

    def _apply(self, data, mtime):
        max_tier = self.max_tier if self.max_tier is not None else int(data.get("MaxTier") or 0)
        entries = []
        for logical in data.get("LogicalServers") or []:
            entry = self._entry(logical, max_tier)
            if entry and entry["country"]:
                entries.append(entry)
        self.entries = entries
        self._mtime = mtime
        # Caches written before ExpirationTime existed fall back to the CLI's own three hour TTL.
        self._expires = float(data.get("ExpirationTime") or (mtime + SERVERLIST_TTL_S))

    def _read(self):
        """Parse the cache file into entries; True when it was readable."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.log(f"server list missing: {self.path}")
            return False
        if self._mtime == mtime and self._expires is not None:
            return True
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as err:
            self.log(f"server list unreadable: {type(err).__name__}")
            return False
        self._apply(data, mtime)
        return True

    def _stale(self):
        return not self.entries or self._expires is None or self.now() >= self._expires

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def _refresh(self):
        """Fetch the server list through the session; the stale cache stays usable when that fails."""
        if self.refresh is None:
            return False
        now = self.now()
        if self._last_refresh is not None and now - self._last_refresh < self.retry_s:
            return False
        self._last_refresh = now
        self.log("server list stale; refreshing through the proton session")
        try:
            data = self.refresh()
        except Exception as err:
            self.log(f"server list refresh failed: {type(err).__name__}")
            return False
        if not isinstance(data, dict) or not data.get("LogicalServers"):
            self.log("server list refresh returned no servers")
            return False
        try:
            self._write(data)
        except OSError as err:
            self.log(f"could not write server list cache: {type(err).__name__}")
            return False
        self._mtime = None
        self._expires = None
        return True

    def load(self, force=False):
        if force:
            self._mtime = None
            self._expires = None
        self._read()
        if not self._stale():
            return True
        if self._refresh():
            self._read()
        return bool(self.entries)

    def countries(self):
        if not self.entries:
            self.load()
        return sorted({e["country"] for e in self.entries})

    def servers(self, country):
        if not self.entries:
            self.load()
        return [e for e in self.entries if e["country"] == country]

    def pick(self, country, skip_servers=None, skip_ips=None, rng=None):
        skip_servers = skip_servers or set()
        skip_ips = skip_ips or set()
        cands = [
            e for e in self.servers(country)
            if e["server"] not in skip_servers and e["entry_ip"] not in skip_ips
        ]
        cands.sort(key=lambda e: (e["load"], e["server"]))
        if not cands:
            return None
        return rng.choice(cands) if rng else cands[0]


class ProtonCredentials:
    """WireGuard key and certificate from the signed-in Proton CLI session."""

    def __init__(self, log=None, session_factory=None, session=None, now_fn=time.time, retry_s=CERT_RETRY_S):
        self.log = log or noop
        self.session = session or ProtonSession(session_factory=session_factory, log=log)
        self.now = now_fn
        self.retry_s = retry_s
        self._last_attempt = 0

    def _pubkey_credentials(self):
        return self.session.get().vpn_account.vpn_credentials.pubkey_credentials

    def wg_private_key(self):
        try:
            return self._pubkey_credentials().wg_private_key or None
        except Exception as err:
            self.log(f"proton session unavailable: {type(err).__name__}")
            return None

    def ensure_certificate(self):
        try:
            remaining = float(self._pubkey_credentials().certificate_validity_remaining or 0)
        except Exception as err:
            self.log(f"proton certificate check failed: {type(err).__name__}")
            return False
        if remaining >= CERT_REFRESH_BELOW_S:
            return True
        now = self.now()
        if now - self._last_attempt < self.retry_s:
            return False
        self._last_attempt = now
        self.log(f"wireguard certificate expires in {int(remaining)}s; refreshing")
        try:
            self.session.fetch_certificate()
            remaining = float(self._pubkey_credentials().certificate_validity_remaining or 0)
        except Exception as err:
            self.log(f"certificate refresh failed: {type(err).__name__}")
            return False
        self.log(f"wireguard certificate refreshed, valid for {int(remaining)}s")
        return remaining > 0


def wait_for_port(port, timeout=START_TIMEOUT_S, host="127.0.0.1"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class WireproxyTunnel:
    """wireproxy child that exposes a local HTTP proxy into a Proton WireGuard server."""

    def __init__(
        self,
        wireproxy_bin="wireproxy",
        port=DEFAULT_PROXY_PORT,
        runtime_dir=None,
        log=None,
        spawn=None,
        wait_port=None,
        start_timeout_s=START_TIMEOUT_S,
    ):
        self.bin = wireproxy_bin
        self.port = int(port)
        self.log = log or noop
        base = runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        self.dir = Path(base) / "vpn-rotate"
        self.spawn = spawn or subprocess.Popen
        self.wait_port = wait_port or wait_for_port
        self.start_timeout_s = start_timeout_s
        self.proc = None
        self.conf_path = None
        self.server = None
        self._output = []
        self._secret = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def proxy_url(self):
        return f"http://127.0.0.1:{self.port}" if self.running() else None

    def _safe(self, text):
        if self._secret and text:
            return text.replace(self._secret, "***")
        return text or ""

    def _drain(self, proc):
        try:
            for line in proc.stdout:
                self._output = (self._output + [self._safe(line.rstrip())])[-20:]
        except (OSError, ValueError):
            pass

    def _write_config(self, private_key, server):
        conf = self.dir / f"wg-{os.getpid()}-{int(time.time() * 1000)}.conf"
        body = (
            "[Interface]\n"
            f"PrivateKey = {private_key}\n"
            f"Address = {NETSTACK_ADDRESS}\n"
            f"DNS = {NETSTACK_DNS}\n"
            f"MTU = {NETSTACK_MTU}\n\n"
            "[Peer]\n"
            f"PublicKey = {server['public_key']}\n"
            f"Endpoint = {server['entry_ip']}:{WIREGUARD_PORT}\n"
            "AllowedIPs = 0.0.0.0/0\n\n"
            "[http]\n"
            f"BindAddress = 127.0.0.1:{self.port}\n"
        )
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
        except OSError as err:
            self.log(f"tunnel config write failed: {err}")
            return None
        self.conf_path = conf
        return conf

    def _cleanup(self):
        if self.conf_path:
            try:
                self.conf_path.unlink()
            except OSError:
                pass
        self.conf_path = None

    def start(self, private_key, server):
        self.stop()
        if not private_key:
            self.log("tunnel start without a wireguard key")
            return False
        self._secret = private_key
        conf = self._write_config(private_key, server)
        if conf is None:
            return False
        try:
            self.proc = self.spawn(
                [self.bin, "-c", str(conf), "-s"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as err:
            self.log(f"wireproxy start failed: {err}")
            self._cleanup()
            return False
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()
        if not self.wait_port(self.port, self.start_timeout_s):
            self.log(f"wireproxy did not open 127.0.0.1:{self.port}: {self._safe(' / '.join(self._output))[:200]}")
            self.stop()
            return False
        self.server = server.get("server")
        return True

    def stop(self):
        proc, self.proc = self.proc, None
        self.server = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        self._cleanup()


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
        self.rotation_model = None
        self.server_strikes = {}
        self.is_rotating = False
        self.mode = "direct"
        self.proxy = None
        self.host_ip_flagged_until = None
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
        self.server_strikes = {
            k: hits for k, v in self.server_strikes.items()
            if (hits := [t for t in v if t > now - STRIKE_WINDOW_S])
        }

    def load(self):
        with self.lock:
            self.clean_expired()
            if not self.state_file.exists():
                return
            try:
                data = json.loads(self.state_file.read_text())
                now = self.now_fn()
                for key, attr in (
                    ("rateLimitedCountries", "rate_limited_countries"),
                    ("rateLimitedServers", "rate_limited_servers"),
                    ("rateLimitedIps", "rate_limited_ips"),
                ):
                    for k, v in (data.get(key) or {}).items():
                        if (v.get("at", 0) + v.get("ttl", DEFAULT_FLAGGED_TTL_S)) > now:
                            getattr(self, attr)[k] = v
                self.current_server = data.get("currentServer") or data.get("server")
                self.current_country = data.get("currentCountry") or data.get("country")
                self.current_location = data.get("currentLocation") or data.get("location")
                self.current_ip = data.get("currentIp") or data.get("egressIp")
                self.last_rotation_time = data.get("lastRotationTime", 0)
                self.last_attempt_at = data.get("lastAttemptAt", 0)
                self.rotations = [t for t in data.get("rotations", []) if t >= now - HOUR_S]
                self.server_strikes = data.get("serverStrikes") or {}
                self.mode = data.get("mode") or "direct"
                self.proxy = data.get("proxy")
                self.host_ip_flagged_until = data.get("hostIpFlaggedUntil")
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
                "serverStrikes": self.server_strikes,
                "mode": self.mode,
                "proxy": self.proxy,
                "hostIpFlaggedUntil": self.host_ip_flagged_until,
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
                "mode": self.mode,
                "proxy": self.proxy,
                "hostIpFlaggedUntil": self.host_ip_flagged_until,
                "server": self.current_server,
                "country": self.current_country,
                "location": self.current_location,
                "egressIp": self.current_ip,
                "allRateLimited": self.all_rate_limited,
                "refreshIn": self.min_refresh_in,
                "model": self.rotation_model,
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
        self.rate_limited_countries[country] = {"at": self.now_fn(), "ttl": ttl, "reason": reason}
        self.save()

    def flag_server(self, server, ip=None, country=None, ttl=DEFAULT_FLAGGED_TTL_S, reason="rate_limit", model=None):
        if not server and not ip:
            return
        now = self.now_fn()
        if server:
            self.rate_limited_servers[flag_key(server, model)] = {
                "at": now,
                "ttl": ttl,
                "ip": ip,
                "country": country or base_country(server),
                "reason": reason,
            }
        if ip:
            self.rate_limited_ips[flag_key(ip, model)] = {
                "at": now,
                "ttl": ttl,
                "server": server,
                "country": country or base_country(server),
                "reason": reason,
            }
        self.save()

    def strike_server(self, server, ip=None, country=None, reason="rate_limit", model=None):
        """Counts a failure; returns True once the server gets flagged."""
        if not server:
            return False
        self.clean_expired()
        key = flag_key(server, model)
        hits = self.server_strikes.get(key, []) + [self.now_fn()]
        if len(hits) >= STRIKES_TO_FLAG:
            self.server_strikes.pop(key, None)
            ttl = DEFAULT_UNREACHABLE_TTL_S if reason == "unreachable" else DEFAULT_FLAGGED_TTL_S
            self.flag_server(server, ip=ip, country=country, ttl=ttl, reason=reason, model=model)
            return True
        self.server_strikes[key] = hits
        self.save()
        return False

    def flag_host_ip(self, ttl=DEFAULT_FLAGGED_TTL_S):
        self.host_ip_flagged_until = self.now_fn() + ttl
        self.save()

    def is_server_rate_limited(self, server, ip=None, model=None):
        self.clean_expired()
        if server and server in flagged_names(self.rate_limited_servers, model):
            return True
        if ip and ip in flagged_names(self.rate_limited_ips, model):
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
        for info in list(self.rate_limited_servers.values()) + list(self.rate_limited_ips.values()):
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
            self.server_strikes.clear()
        self.save()


class VpnRotator:
    def __init__(
        self,
        state,
        log,
        catalog=None,
        credentials=None,
        tunnel=None,
        countries_allowlist=None,
        countries_blocklist=None,
        cooldown_s=600,
        max_per_hour=3,
        rotate_budget_s=ROTATE_BUDGET_S,
        direct_ttl_s=DEFAULT_DIRECT_TTL_S,
        now_fn=time.time,
        sleep_fn=time.sleep,
        probe=None,
        get_ip=None,
        bun_bin=None,
        rng=None,
    ):
        self.rng = rng or random.Random()
        self.cancel_event = threading.Event()
        self.state = state
        self.log = log or noop
        self.catalog = catalog or ServerCatalog(log=self.log, refresh=ProtonSession(log=self.log).fetch_server_list)
        self.credentials = credentials or ProtonCredentials(log=self.log)
        self.tunnel = tunnel or WireproxyTunnel(log=self.log)
        self.countries_allowlist = [c.upper() for c in countries_allowlist] if countries_allowlist else []
        self.countries_blocklist = [c.upper() for c in countries_blocklist] if countries_blocklist else []
        self.cooldown_s = cooldown_s
        self.max_per_hour = max_per_hour
        self.rotate_budget_s = rotate_budget_s
        self.direct_ttl_s = direct_ttl_s
        self.now = now_fn
        self.sleep = sleep_fn
        self.probe = probe
        self.get_ip = get_ip
        self.bun_bin = bun_bin or os.environ.get("VPN_ROTATE_BUN") or shutil.which("bun") or "bun"
        self.lock = threading.Lock()

    def proxy_env(self):
        url = self.tunnel.proxy_url
        return {key: url for key in PROXY_ENV_KEYS} if url else {}

    def _get_ip(self, proxy=False):
        if self.get_ip is not None:
            return self.get_ip(proxy)
        return fetch_egress_ip(self.bun_bin, self.proxy_env() if proxy else {})

    def get_live_status(self):
        if self.tunnel.running():
            return {
                "connected": True,
                "server": self.state.current_server,
                "country": self.state.current_country,
                "location": self.state.current_location,
                "egressIp": self.state.current_ip,
            }
        return {"connected": False, "server": None, "country": None, "location": None, "egressIp": self.state.current_ip}

    def _to_direct(self, clear_host_flag=False, bump_rotation=False):
        self.tunnel.stop()
        self.state.mode = "direct"
        self.state.proxy = None
        self.state.current_server = None
        self.state.current_country = None
        self.state.current_location = None
        if clear_host_flag:
            self.state.host_ip_flagged_until = None
        if bump_rotation:
            self.state.last_rotation_time = self.now()
        host_ip = self._get_ip(False)
        if host_ip:
            self.state.current_ip = host_ip
        self.state.save()

    def return_to_direct(self, bump_rotation=True):
        self.log("returning to direct (host network)")
        self._to_direct(clear_host_flag=True, bump_rotation=bump_rotation)

    def startup(self):
        # The tunnel child died with the previous service process, so state must not claim a tunnel.
        self.tunnel.stop()
        self.state.mode = "direct"
        self.state.proxy = None
        self.state.is_rotating = False
        host_ip = self._get_ip(False)
        if host_ip:
            self.state.current_ip = host_ip
        self.state.save()

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.state.mode == "tunnel":
                # A long tunnel session must not outlive its wireguard certificate.
                self.credentials.ensure_certificate()
                if not self.tunnel.running():
                    self.log("tunnel is gone; falling back to direct")
                    self._to_direct(clear_host_flag=False)
                    return
            until = self.state.host_ip_flagged_until
            if until and self.now() >= until:
                self.log("host IP flag expired; back to direct")
                self._to_direct(clear_host_flag=True, bump_rotation=True)
        finally:
            self.lock.release()

    def _build_probe(self, req):
        if self.probe is not None:
            return self.probe
        if req.get("provider") not in ("opencode-free", "opencode"):
            return None
        bun_path = self.bun_bin if (shutil.which(self.bun_bin) or os.path.exists(self.bun_bin)) else None
        if not bun_path:
            return None
        # None lets the probe pick a model the cached catalog still serves.
        model = (req.get("provider") == "opencode-free" and req.get("model")) or None
        return lambda env: probe_opencode(bun_bin=bun_path, model=model, env=env)

    def rotate(self, req):
        with self.lock:
            self.cancel_event.clear()
            # The status row names the model while rotating and after a failed rotation.
            self.state.rotation_model = req.get("model") or None
            self.state.save()
            return self._rotate(req)

    def cancel(self):
        """Stops a running rotation before its next server; returns whether one was running."""
        running = self.lock.locked()
        if running:
            self.cancel_event.set()
        return running

    def _rotate(self, req):
        err_msg = req.get("errorMessage", "")
        if USAGE_LIMIT_RE.search(err_msg):
            self.log("account limit encountered; skipping rotation")
            return {"ok": False, "reason": "account limit"}

        now = self.now()
        retry = req.get("retry", 0)

        cutoff = now - HOUR_S
        recent = [t for t in self.state.rotations if t >= cutoff]
        if self.max_per_hour > 0 and len(recent) >= self.max_per_hour:
            self.log(f"hourly rotation cap reached ({len(recent)}/{self.max_per_hour})")
            return {"ok": False, "reason": "hourly cap"}

        if retry == 0 and self.state.last_attempt_at:
            elapsed = now - self.state.last_attempt_at
            if elapsed < self.cooldown_s:
                self.log(f"cooldown in effect ({int(self.cooldown_s - elapsed)}s remaining)")
                return {"ok": False, "reason": "cool-down"}

        deadline = now + self.rotate_budget_s
        flag_current = req.get("errorKind") in ("rate_limit", "usage_limit")

        if not self.catalog.load():
            self.log("server list unavailable")
            return {"ok": False, "reason": "server list unavailable"}

        model = req.get("model")
        failed_servers = set()
        if self.state.mode == "tunnel":
            if flag_current:
                server = self.state.current_server or self.tunnel.server
                self.log(f"strike on current server {server} ({self.state.current_ip})")
                self.state.strike_server(
                    server, ip=self.state.current_ip, country=self.state.current_country, model=model
                )
                failed_servers.add(server)
        else:
            host_ip = self._get_ip(False)
            self.log(f"flagging host IP {host_ip} as rate-limited for {self.direct_ttl_s}s")
            if host_ip:
                self.state.current_ip = host_ip
            self.state.flag_host_ip(self.direct_ttl_s)

        all_countries = self.catalog.countries()
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

        single_country_mode = len(pool) == 1
        probe_fn = self._build_probe(req)
        tried_countries = set()
        private_key = None
        success = None

        def fail(entry, ip, country, reason, message, ttl=None):
            # A dead server is dead for every model; a 429 only counts against this model.
            scope = model if reason == "rate_limit" else None
            if ttl is not None:
                # The provider named its reset, so flag until then instead of re-probing every 15 min.
                self.state.flag_server(entry["server"], ip=ip, country=country, ttl=ttl, reason=reason, model=scope)
                outcome = f"flagged for {ttl}s"
            else:
                flagged = self.state.strike_server(entry["server"], ip=ip, country=country, reason=reason, model=scope)
                outcome = "flagged" if flagged else "strike"
            self.log(f"{message}; {outcome}, trying next")
            failed_servers.add(entry["server"])
            # One server per country per pass spreads the attempts over more IPs.
            if not single_country_mode:
                tried_countries.add(country)

        # Refresh the certificate up front so a long tunnel does not start with one about to expire.
        self.credentials.ensure_certificate()

        while True:
            if self.cancel_event.is_set():
                self.log("rotation cancelled")
                self._to_direct(clear_host_flag=False)
                return {"ok": False, "reason": "cancelled", "mode": self.state.mode}
            if self.now() + CANDIDATE_COST_S > deadline:
                self.log("rotation time budget exhausted")
                break

            untried = [c for c in pool if c not in tried_countries]
            if not untried:
                break
            target = self.rng.choice(untried)

            entry = self.catalog.pick(
                target,
                skip_servers=flagged_names(self.state.rate_limited_servers, model) | failed_servers,
                skip_ips=flagged_names(self.state.rate_limited_ips, model),
                rng=self.rng,
            )
            if entry is None:
                tried_countries.add(target)
                continue

            if private_key is None:
                private_key = self.credentials.wg_private_key()
                if not private_key:
                    self.log("no wireguard key from the proton session")
                    return {"ok": False, "reason": "no wireguard key"}

            if not self.tunnel.start(private_key, entry):
                fail(entry, None, target, "unreachable", f"tunnel to {entry['server']} ({target}) failed to start")
                continue

            egress_ip = self._get_ip(True)
            if not egress_ip:
                fail(entry, None, target, "unreachable", f"no egress IP through {entry['server']}")
                continue

            if self.state.is_server_rate_limited(entry["server"], egress_ip, model):
                self.log(f"server {entry['server']} ({egress_ip}) is known rate-limited; trying next")
                failed_servers.add(entry["server"])
                continue

            if probe_fn:
                self.sleep(SETTLE_S)
                p_res = probe_fn(self.proxy_env())
                probe_status, _, detail = str(p_res).partition("\t")
                if p_res is True or probe_status == "200":
                    pass
                elif probe_status == "missing_bun":
                    self.log("bun not found; skipping probe")
                elif probe_status == "skip":
                    self.log("no free-models module or cached catalog; skipping probe")
                elif probe_status == "429" or p_res is False:
                    message = f"target {target} ({entry['server']}) probe returned 429 {detail}".rstrip()
                    fail(entry, egress_ip, target, "rate_limit", message, ttl=retry_after(detail))
                    continue
                elif probe_status in ("0", "timeout"):
                    fail(entry, egress_ip, target, "unreachable", f"target {target} ({entry['server']}) probe timed out")
                    continue
                elif probe_status in ("401", "403"):
                    self.log(f"target {target} ({entry['server']}) probe auth error {probe_status} {detail}; stopping")
                    return {"ok": False, "reason": f"auth error {probe_status}"}
                elif probe_status.startswith("5"):
                    self.log(f"target {target} ({entry['server']}) probe server error {probe_status} {detail}; stopping")
                    return {"ok": False, "reason": f"server error {probe_status}"}

            success = {
                "server": entry["server"],
                "country": target,
                "location": entry.get("city") or target,
                "egressIp": egress_ip,
            }
            break

        if success:
            now_t = self.now()
            self.state.current_server = success["server"]
            self.state.current_country = success["country"]
            self.state.current_location = success["location"]
            self.state.current_ip = success["egressIp"]
            self.state.mode = "tunnel"
            self.state.proxy = self.tunnel.proxy_url or f"http://127.0.0.1:{self.tunnel.port}"
            self.state.last_rotation_time = now_t
            self.state.last_attempt_at = now_t
            self.state.rotations.append(now_t)
            self.state.all_rate_limited = False
            self.state.min_refresh_in = 0
            self.state.save()
            self.log(f"tunnelling through {success['server']} ({success['country']}) egress {success['egressIp']}")
            return {
                "ok": True,
                "mode": "tunnel",
                "proxy": self.state.proxy,
                "hostIpFlaggedUntil": self.state.host_ip_flagged_until,
                "server": success["server"],
                "country": success["country"],
                "location": success["location"],
                "egressIp": success["egressIp"],
            }

        # Struck servers are eligible again at once, so report a short retry instead of the longest flag.
        min_refresh = min(self.state.get_min_refresh_in(pool) or ALL_FAILED_RETRY_S, ALL_FAILED_RETRY_S)
        self.state.all_rate_limited = True
        self.state.min_refresh_in = min_refresh
        self._to_direct(clear_host_flag=False)
        self.log(f"all available servers/countries are rate-limited; next refresh in {min_refresh}s")
        return {
            "ok": False,
            "reason": "all_rate_limited",
            "mode": self.state.mode,
            "refreshIn": min_refresh,
            "rateLimitedCountries": self.state.get_rate_limited_countries_list(),
        }

    def clear(self):
        self.cancel()
        with self.lock:
            self.return_to_direct()
            self.state.clear()
        return {"ok": True, "cleared": True, "mode": self.state.mode}

    def shutdown(self):
        self.tunnel.stop()


class VpnService:
    def __init__(self, rotator, sock_path=None, tick_interval_s=TICK_S):
        self.rotator = rotator
        if sock_path:
            self.sock_path = Path(sock_path)
        else:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            self.sock_path = Path(runtime_dir) / "vpn-rotate.sock"
        self.rotate_lock = asyncio.Lock()
        self.tick_interval_s = tick_interval_s
        self.server = None
        self._tick_task = None

    def _try_debounce(self, session_id):
        state = self.rotator.state
        if state.mode != "tunnel" or not state.current_server:
            return None
        if self.rotator.now() - state.last_rotation_time >= DEBOUNCE_S:
            return None
        if session_id and session_id not in state.debounced_sessions:
            state.debounced_sessions.append(session_id)
            state.save()
        return {
            "ok": True,
            "debounced": True,
            "mode": state.mode,
            "proxy": state.proxy,
            "hostIpFlaggedUntil": state.host_ip_flagged_until,
            "server": state.current_server,
            "country": state.current_country,
            "location": state.current_location,
            "egressIp": state.current_ip,
        }

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
            "mode": s.mode,
            "proxy": s.proxy,
            "hostIpFlaggedUntil": s.host_ip_flagged_until,
            "server": live.get("server"),
            "country": live.get("country"),
            "location": live.get("location"),
            "egressIp": live.get("egressIp"),
            "allRateLimited": s.all_rate_limited,
            "refreshIn": s.min_refresh_in,
            "rateLimitedCountries": s.get_rate_limited_countries_list(),
            "rotating": s.is_rotating,
        }

    async def handle_clear(self):
        return await asyncio.to_thread(self.rotator.clear)

    async def handle_cancel(self):
        return {"ok": True, "cancelled": self.rotator.cancel()}

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
            elif action == "cancel":
                resp = await self.handle_cancel()
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

    async def _tick_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.rotator.tick)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.rotator.log(f"tick failed: {type(err).__name__}: {err}")
            await asyncio.sleep(self.tick_interval_s)

    async def start(self):
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass
        self.server = await asyncio.start_unix_server(self.handle_client, path=str(self.sock_path))
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self):
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
            self._tick_task = None
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
        self.rotator.shutdown()


async def run_service(service, log):
    await service.start()
    log(f"service listening on {service.sock_path} (mode={service.rotator.state.mode})")
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
    state_dir = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "vpn-rotate"
    # Keep the rate-limit history from before the rename to vpn-rotate.
    old_state_dir = state_dir.with_name("pi-vpn-rotate")
    if old_state_dir.is_dir() and not state_dir.exists():
        old_state_dir.rename(state_dir)
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

    state_file = state_dir / "state.json"
    runtime_file = runtime_dir / "vpn-rotate.json"
    sock_path = runtime_dir / "vpn-rotate.sock"
    log_file = state_dir / "service.log"

    log = Log(log_file)
    state = VpnState(state_file, runtime_file)

    cooldown_s = int(os.environ.get("VPN_ROTATE_COOLDOWN_S", "600"))
    max_per_hour = int(os.environ.get("VPN_ROTATE_MAX_PER_HOUR", "3"))
    direct_ttl_s = int(os.environ.get("VPN_ROTATE_DIRECT_TTL_S", str(DEFAULT_DIRECT_TTL_S)))
    proxy_port = int(os.environ.get("VPN_ROTATE_PROXY_PORT", str(DEFAULT_PROXY_PORT)))
    wireproxy = os.environ.get("VPN_ROTATE_WIREPROXY") or shutil.which("wireproxy") or "wireproxy"
    serverlist = os.environ.get("VPN_ROTATE_SERVERLIST") or DEFAULT_SERVERLIST

    raw_countries = os.environ.get("VPN_ROTATE_COUNTRIES", "")
    countries = [c.strip().upper() for c in raw_countries.split(",") if c.strip()]
    raw_excluded = os.environ.get("VPN_ROTATE_EXCLUDED_COUNTRIES", "")
    excluded = [c.strip().upper() for c in raw_excluded.split(",") if c.strip()]

    if not (os.path.exists(wireproxy) or shutil.which(wireproxy)):
        log(f"wireproxy binary not found: {wireproxy}")

    proton = ProtonSession(log=log)
    catalog = ServerCatalog(serverlist, log=log, refresh=proton.fetch_server_list)
    tunnel = WireproxyTunnel(wireproxy, port=proxy_port, runtime_dir=runtime_dir, log=log)
    rotator = VpnRotator(
        state=state,
        log=log,
        catalog=catalog,
        credentials=ProtonCredentials(log=log, session=proton),
        tunnel=tunnel,
        countries_allowlist=countries,
        countries_blocklist=excluded,
        cooldown_s=cooldown_s,
        max_per_hour=max_per_hour,
        direct_ttl_s=direct_ttl_s,
    )
    rotator.startup()

    service = VpnService(rotator, sock_path=sock_path)
    try:
        asyncio.run(run_service(service, log))
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        rotator.shutdown()


if __name__ == "__main__":
    main()
