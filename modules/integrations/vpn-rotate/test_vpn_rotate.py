"""Tests for pi-agent VPN rotation service and client."""
import asyncio
import io
import json
import os
import stat
import types

import vpn_rotate as client
import vpn_rotate_service as service

HOST_IP = "100.0.0.1"
KEY = "k" * 43 + "="


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeTunnel:
    def __init__(self, port=25345, fail=()):
        self.port = port
        self.fail = set(fail)
        self.starts = []
        self.stops = 0
        self.running_flag = False
        self.server = None

    def running(self):
        return self.running_flag

    @property
    def proxy_url(self):
        return f"http://127.0.0.1:{self.port}" if self.running_flag else None

    def start(self, private_key, entry):
        self.starts.append((private_key, entry["server"]))
        if entry["server"] in self.fail:
            self.running_flag = False
            self.server = None
            return False
        self.running_flag = True
        self.server = entry["server"]
        return True

    def stop(self):
        if self.running_flag:
            self.stops += 1
        self.running_flag = False
        self.server = None


class FakeCredentials:
    def __init__(self, key=KEY):
        self.key = key
        self.cert_calls = 0

    def wg_private_key(self):
        return self.key

    def ensure_certificate(self):
        self.cert_calls += 1
        return True


def logical(name, country, load=10, tier=2, features=0, status=1, ips=None, sub_status=1, city="City"):
    ips = ips or [f"10.1.{load}.1"]
    return {
        "Name": name,
        "ExitCountry": country,
        "City": city,
        "Load": load,
        "Tier": tier,
        "Features": features,
        "Status": status,
        "Servers": [
            {"EntryIP": ip, "X25519PublicKey": f"PUB{name}", "Status": sub_status} for ip in ips
        ],
    }


DEFAULT_LOGICALS = [
    logical("NL#1", "NL", load=20, ips=["190.2.132.1"]),
    logical("NL#2", "NL", load=5, ips=["190.2.132.2"]),
    logical("DE#1", "DE", load=10, ips=["149.88.102.1"]),
    logical("UK#1", "UK", load=1, features=service.FEATURE_SECURE_CORE),
    logical("SE#1", "SE", load=1, features=service.FEATURE_TOR),
    logical("FR#1", "FR", load=1, tier=9),
    logical("JP#1", "JP", load=1, status=0),
    logical("JP#2", "JP", load=1, sub_status=0),
]


def write_serverlist(path, logicals=None, max_tier=2):
    path.write_text(json.dumps({"MaxTier": max_tier, "LogicalServers": logicals or DEFAULT_LOGICALS}))
    return path


def make_state(tmp_path, clock):
    return service.VpnState(tmp_path / "state.json", tmp_path / "runtime.json", now_fn=clock.now)


class FirstRng:
    """Deterministic stand-in for random: always the first candidate."""

    def choice(self, seq):
        return seq[0]


def make_rotator(tmp_path, clock=None, serverlist=None, tunnel=None, credentials=None,
                 probe=None, ips=None, **kwargs):
    clock = clock or FakeClock()
    state = make_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    path = serverlist or write_serverlist(tmp_path / "serverlist.json")
    catalog = service.ServerCatalog(path, log=log)
    tunnel = tunnel or FakeTunnel()
    credentials = credentials or FakeCredentials()
    ips = ips or {}
    calls = []

    def default_probe(_env):
        return "200"

    def get_ip(proxy):
        calls.append(proxy)
        if not proxy:
            return HOST_IP
        return ips.get(tunnel.server, f"200.9.9.{len(tunnel.starts)}")

    kwargs.setdefault("cooldown_s", 0)
    kwargs.setdefault("rng", FirstRng())
    kwargs.setdefault("max_per_hour", 10)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        catalog=catalog,
        credentials=credentials,
        tunnel=tunnel,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        probe=probe or default_probe,
        get_ip=get_ip,
        **kwargs,
    )
    return rotator, state, tunnel, calls


# Catalog

def test_catalog_filters_secure_core_tor_free_tier_and_down_servers(tmp_path):
    catalog = service.ServerCatalog(write_serverlist(tmp_path / "sl.json"))
    assert catalog.load() is True
    assert catalog.countries() == ["DE", "NL"]
    assert [e["server"] for e in catalog.servers("NL")] == ["NL#1", "NL#2"]


def test_catalog_picks_lowest_load_and_skips_flagged(tmp_path):
    catalog = service.ServerCatalog(write_serverlist(tmp_path / "sl.json"))
    assert catalog.pick("NL")["server"] == "NL#2"
    assert catalog.pick("NL", skip_servers={"NL#2"})["server"] == "NL#1"
    assert catalog.pick("NL", skip_servers={"NL#1", "NL#2"}) is None
    assert catalog.pick("NL", skip_ips={"190.2.132.2"})["server"] == "NL#1"


def test_catalog_missing_or_broken_file(tmp_path, capsys):
    catalog = service.ServerCatalog(tmp_path / "nope.json")
    assert catalog.load() is False
    assert catalog.countries() == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert service.ServerCatalog(broken).load() is False


def test_catalog_entry_carries_wireguard_peer_data(tmp_path):
    catalog = service.ServerCatalog(write_serverlist(tmp_path / "sl.json"))
    entry = catalog.pick("DE")
    assert entry["entry_ip"] == "149.88.102.1"
    assert entry["public_key"] == "PUBDE#1"
    assert entry["country"] == "DE"


# Server list refresh

class FakeServerListResult:
    def __init__(self, logicals=None, expiration=None):
        self.logicals = logicals or DEFAULT_LOGICALS
        self.expiration = expiration

    def to_dict(self):
        return {"MaxTier": 2, "ExpirationTime": self.expiration, "LogicalServers": self.logicals}


def make_catalog(path, clock, refresh=None):
    log = service.Log(path.parent / "catalog.log", now_fn=clock.now)
    return service.ServerCatalog(path, log=log, refresh=refresh, now_fn=clock.now)


def test_catalog_refreshes_expired_cache_through_session(tmp_path):
    clock = FakeClock()
    path = tmp_path / "serverlist.json"
    path.write_text(json.dumps({
        "MaxTier": 2, "ExpirationTime": 1,
        "LogicalServers": [logical("NL#9", "NL", load=1, ips=["9.9.9.9"])],
    }))
    calls = []

    def refresh():
        calls.append(clock.now())
        return FakeServerListResult(expiration=clock.now() + 3600).to_dict()

    catalog = make_catalog(path, clock, refresh=refresh)
    assert catalog.load() is True
    assert calls == [clock.now()]
    assert catalog.countries() == ["DE", "NL"]
    assert json.loads(path.read_text())["ExpirationTime"] == clock.now() + 3600
    assert catalog.load() is True
    assert len(calls) == 1


def test_catalog_refreshes_missing_cache(tmp_path):
    clock = FakeClock()
    path = tmp_path / "missing.json"
    catalog = make_catalog(
        path, clock, refresh=lambda: FakeServerListResult(expiration=clock.now() + 60).to_dict()
    )
    assert catalog.load() is True
    assert path.exists()
    assert catalog.pick("DE")["server"] == "DE#1"


def test_catalog_keeps_stale_cache_when_refresh_fails(tmp_path):
    clock = FakeClock()
    path = tmp_path / "serverlist.json"
    path.write_text(json.dumps({"MaxTier": 2, "ExpirationTime": 1, "LogicalServers": DEFAULT_LOGICALS}))
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("offline")

    catalog = service.ServerCatalog(path, refresh=boom, now_fn=clock.now, retry_s=600)
    assert catalog.load() is True
    assert catalog.countries() == ["DE", "NL"]
    assert len(calls) == 1
    assert catalog.load() is True
    assert len(calls) == 1
    clock.t += 601
    catalog.load()
    assert len(calls) == 2


def test_catalog_ignores_empty_refresh_result(tmp_path):
    clock = FakeClock()
    path = tmp_path / "serverlist.json"
    path.write_text(json.dumps({"MaxTier": 2, "ExpirationTime": 1, "LogicalServers": DEFAULT_LOGICALS}))
    catalog = make_catalog(path, clock, refresh=lambda: {"LogicalServers": []})
    assert catalog.load() is True
    assert json.loads(path.read_text())["LogicalServers"] == DEFAULT_LOGICALS


def test_catalog_without_refresh_still_uses_expired_cache(tmp_path):
    clock = FakeClock()
    path = tmp_path / "serverlist.json"
    path.write_text(json.dumps({"MaxTier": 2, "ExpirationTime": 1, "LogicalServers": DEFAULT_LOGICALS}))
    catalog = service.ServerCatalog(path, now_fn=clock.now)
    assert catalog.load() is True
    assert catalog.pick("NL")["server"] == "NL#2"


def test_catalog_treats_cache_without_expiration_as_fresh_three_hours(tmp_path):
    clock = FakeClock()
    path = write_serverlist(tmp_path / "serverlist.json")
    os.utime(path, (clock.now(), clock.now()))
    calls = []
    catalog = make_catalog(path, clock, refresh=lambda: calls.append(1))
    assert catalog.load() is True
    assert calls == []
    clock.t += 3 * 3600 + 1
    catalog.load()
    assert calls == [1]


# Rotation

def test_rotate_direct_to_tunnel(tmp_path):
    rotator, state, tunnel, calls = make_rotator(tmp_path, ips={"DE#1": "200.1.1.1"})
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["mode"] == "tunnel"
    assert state.mode == "tunnel"
    assert state.proxy == "http://127.0.0.1:25345"
    assert state.current_server == "DE#1"
    assert state.current_country == "DE"
    assert state.current_ip == "200.1.1.1"
    assert state.host_ip_flagged_until == 1000.0 + 3600
    assert tunnel.starts == [(KEY, "DE#1")]
    assert calls == [False, True]
    assert len(state.rotations) == 1


def test_rotate_probe_receives_proxy_environment(tmp_path):
    seen = []
    rotator, state, tunnel, _ = make_rotator(tmp_path, probe=lambda env: seen.append(dict(env)) or "200")
    rotator.rotate({"errorKind": "rate_limit"})
    assert seen and seen[0]["HTTPS_PROXY"] == "http://127.0.0.1:25345"
    assert seen[0]["http_proxy"] == seen[0]["HTTPS_PROXY"]


def test_rotate_probe_429_flags_until_the_reported_reset(tmp_path):
    answers = iter(['429\t{"retryAfter": "86000", "body": "Free usage limit reached"}', "200"])
    rotator, state, _, _ = make_rotator(
        tmp_path, probe=lambda env: next(answers), ips={"DE#1": "200.1.1.1", "NL#2": "200.1.1.2"}
    )
    res = rotator.rotate({"errorKind": "rate_limit", "model": "m"})
    assert res["ok"] is True and res["server"] == "NL#2"
    log = (tmp_path / "test.log").read_text()
    assert 'probe returned 429 {"retryAfter": "86000", "body": "Free usage limit reached"}; flagged for 86000s' in log
    assert state.rate_limited_servers["DE#1|m"]["ttl"] == 86000
    assert state.rate_limited_ips["200.1.1.1|m"]["ttl"] == 86000
    assert "DE#1|m" not in state.server_strikes


def test_retry_after_reads_only_positive_seconds():
    assert service.retry_after('{"retryAfter": "120"}') == 120
    assert service.retry_after('{"retryAfter": null}') is None
    assert service.retry_after('{"retryAfter": "Wed, 21 Oct 2026 07:28:00 GMT"}') is None
    assert service.retry_after('{"retryAfter": "0"}') is None
    assert service.retry_after("") is None


def test_rotate_tunnel_to_tunnel_strikes_current_server(tmp_path):
    rotator, state, tunnel, _ = make_rotator(
        tmp_path, ips={"DE#1": "200.1.1.1", "NL#2": "200.1.1.2"}
    )
    rotator.rotate({"errorKind": "rate_limit"})
    tunnel.proxy_url  # tunnel is up
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["server"] == "NL#2"
    assert len(state.server_strikes["DE#1"]) == 1
    assert "DE#1" not in state.rate_limited_servers
    assert [s[1] for s in tunnel.starts] == ["DE#1", "NL#2"]
    assert state.mode == "tunnel"


def test_rotate_probe_429_strikes_server_then_moves_country(tmp_path):
    answers = iter(["429", "200"])
    rotator, state, tunnel, _ = make_rotator(
        tmp_path, probe=lambda env: next(answers), ips={"DE#1": "200.1.1.1", "NL#2": "200.1.1.2"}
    )
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["country"] == "NL"
    assert "DE" not in state.rate_limited_countries
    assert len(state.server_strikes["DE#1"]) == 1
    assert "DE#1" not in state.rate_limited_servers
    assert [s[1] for s in tunnel.starts] == ["DE#1", "NL#2"]


def test_rotate_single_country_falls_back_to_next_server(tmp_path):
    logicals = [logical("NL#1", "NL", load=1, ips=["190.2.132.1"]),
                logical("NL#2", "NL", load=5, ips=["190.2.132.2"])]
    tunnel = FakeTunnel(fail={"NL#1"})
    rotator, state, _, _ = make_rotator(
        tmp_path, serverlist=write_serverlist(tmp_path / "sl.json", logicals), tunnel=tunnel
    )
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["server"] == "NL#2"
    assert [s[1] for s in tunnel.starts] == ["NL#1", "NL#2"]


def test_rotate_all_rate_limited_stops_tunnel_and_returns_direct(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, probe=lambda env: "429")
    res = rotator.rotate({"errorKind": "rate_limit", "model": "m-free"})
    assert json.loads((tmp_path / "runtime.json").read_text())["model"] == "m-free"
    assert res["ok"] is False
    assert res["reason"] == "all_rate_limited"
    assert res["refreshIn"] > 0
    assert state.all_rate_limited is True
    assert state.mode == "direct"
    assert state.proxy is None
    assert state.current_server is None
    assert tunnel.running() is False
    assert res["refreshIn"] == service.ALL_FAILED_RETRY_S
    assert state.rate_limited_countries == {}
    assert set(state.server_strikes) == {"DE#1|m-free", "NL#2|m-free"}


def test_rotate_skips_flagged_servers(tmp_path):
    rotator, state, tunnel, calls = make_rotator(tmp_path)
    state.flag_server("DE#1")
    state.flag_server("NL#1")
    state.flag_server("NL#2")
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["reason"] == "all_rate_limited"
    assert tunnel.starts == []


def test_three_strikes_in_window_flag_server_for_fifteen_minutes(tmp_path):
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    assert state.strike_server("DE#1", ip="200.1.1.1") is False
    clock.t += service.STRIKE_WINDOW_S + 1
    assert state.strike_server("DE#1", ip="200.1.1.1") is False
    assert state.strike_server("DE#1", ip="200.1.1.1") is False
    assert "DE#1" not in state.rate_limited_servers
    assert state.strike_server("DE#1", ip="200.1.1.1") is True
    assert state.rate_limited_servers["DE#1"]["ttl"] == 900
    assert "200.1.1.1" in state.rate_limited_ips
    assert "DE#1" not in state.server_strikes
    assert make_state(tmp_path, clock).rate_limited_servers.keys() == {"DE#1"}


def test_flags_are_per_model(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, ips={"DE#1": "200.1.1.1", "NL#2": "200.1.1.2"})
    for _ in range(service.STRIKES_TO_FLAG):
        state.strike_server("DE#1", ip="200.1.1.1", model="a-free")
    assert state.is_server_rate_limited("DE#1", model="a-free")
    assert not state.is_server_rate_limited("DE#1", "200.1.1.1", model="b-free")
    assert rotator.rotate({"errorKind": "rate_limit", "model": "b-free"})["server"] == "DE#1"


def test_flag_without_model_covers_every_model(tmp_path):
    state = make_state(tmp_path, FakeClock())
    state.flag_server("DE#1", ip="200.1.1.1")
    assert state.is_server_rate_limited("DE#1", model="a-free")
    assert state.is_server_rate_limited(None, "200.1.1.1", model="b-free")


def test_unreachable_strikes_ignore_model(tmp_path):
    tunnel = FakeTunnel(fail={"DE#1"})
    rotator, state, _, _ = make_rotator(tmp_path, tunnel=tunnel)
    rotator.rotate({"errorKind": "rate_limit", "model": "a-free"})
    assert "DE#1" in state.server_strikes


def test_cancel_stops_rotation_before_next_server(tmp_path):
    rotator = None

    def probe(_env):
        assert rotator.cancel() is True
        return "429"

    rotator, state, tunnel, _ = make_rotator(tmp_path, probe=probe)
    res = rotator.rotate({"errorKind": "rate_limit", "model": "m-free"})
    assert res["reason"] == "cancelled"
    assert len(tunnel.starts) == 1
    assert state.mode == "direct"
    assert state.all_rate_limited is False
    assert rotator.cancel() is False


def test_clear_cancels_a_running_rotation(tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()

    def probe(_env):
        started.set()
        release.wait(5)
        return "429"

    rotator, state, tunnel, _ = make_rotator(tmp_path, probe=probe)
    worker = threading.Thread(target=rotator.rotate, args=({"errorKind": "rate_limit"},))
    worker.start()
    assert started.wait(5)
    clearer = threading.Thread(target=rotator.clear)
    clearer.start()
    release.set()
    worker.join(5)
    clearer.join(5)
    assert len(tunnel.starts) == 1
    assert state.server_strikes == {}


def test_rotate_picks_countries_through_rng(tmp_path):
    class LastRng:
        def choice(self, seq):
            return seq[-1]

    rotator, state, tunnel, _ = make_rotator(tmp_path, rng=LastRng())
    assert rotator.rotate({"errorKind": "rate_limit"})["country"] == "NL"


def test_rotate_hourly_cap(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, max_per_hour=2)
    state.rotations = [990.0, 995.0]
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["reason"] == "hourly cap"
    assert tunnel.starts == []


def test_rotate_cooldown_blocks_initial_attempt_only(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, cooldown_s=600)
    state.last_attempt_at = 900.0
    assert rotator.rotate({"errorKind": "rate_limit"})["reason"] == "cool-down"
    assert tunnel.starts == []
    assert rotator.rotate({"errorKind": "rate_limit", "retry": 1})["ok"] is True


def test_rotate_skips_account_limited_errors(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path)
    res = rotator.rotate({"errorMessage": "Monthly usage limit reached"})
    assert res["reason"] == "account limit"
    assert state.mode == "direct"
    assert tunnel.starts == []


def test_rotate_allowlist_and_blocklist(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, countries_allowlist=["NL", "DE"], countries_blocklist=["DE"])
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["country"] == "NL"
    second = tmp_path / "second"
    second.mkdir()
    rotator2, _, _, _ = make_rotator(second, countries_allowlist=["UK"])
    assert rotator2.rotate({"errorKind": "rate_limit"})["reason"] == (
        "no countries left after allowlist and blocklist"
    )


def test_rotate_without_wireguard_key_fails(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, credentials=FakeCredentials(key=None))
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["reason"] == "no wireguard key"
    assert tunnel.starts == []


def test_rotate_without_server_list_fails(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path, serverlist=tmp_path / "missing.json")
    assert rotator.rotate({"errorKind": "rate_limit"})["reason"] == "server list unavailable"


# Timer ticks

def test_tick_returns_to_direct_when_host_flag_expires(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, calls = make_rotator(tmp_path, clock=clock)
    rotator.rotate({"errorKind": "rate_limit"})
    assert state.mode == "tunnel"
    clock.t += 3601
    rotator.tick()
    assert state.mode == "direct"
    assert state.proxy is None
    assert state.host_ip_flagged_until is None
    assert state.current_server is None
    assert state.last_rotation_time == clock.t
    assert tunnel.stops == 1
    assert calls[-1] is False


def test_tick_detects_dead_tunnel(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock)
    rotator.rotate({"errorKind": "rate_limit"})
    tunnel.running_flag = False
    rotator.tick()
    assert state.mode == "direct"
    assert state.proxy is None
    assert state.host_ip_flagged_until is not None


def test_tick_refreshes_certificate_only_while_tunnelled(tmp_path):
    clock = FakeClock()
    credentials = FakeCredentials()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock, credentials=credentials)
    rotator.tick()
    assert credentials.cert_calls == 0
    assert state.mode == "direct"
    assert tunnel.starts == []
    rotator.rotate({"errorKind": "rate_limit"})
    assert credentials.cert_calls == 1
    rotator.tick()
    assert credentials.cert_calls == 2
    tunnel.running_flag = False
    rotator.tick()
    assert credentials.cert_calls == 3
    assert state.mode == "direct"
    rotator.tick()
    assert credentials.cert_calls == 3


def test_rotate_refreshes_certificate_before_starting_tunnel(tmp_path):
    clock = FakeClock()
    events = []

    class OrderedCredentials(FakeCredentials):
        def ensure_certificate(self):
            events.append("cert")
            return super().ensure_certificate()

    class OrderedTunnel(FakeTunnel):
        def start(self, private_key, entry):
            events.append("start")
            return super().start(private_key, entry)

    rotator, _, _, _ = make_rotator(
        tmp_path, clock=clock, credentials=OrderedCredentials(), tunnel=OrderedTunnel()
    )
    assert rotator.rotate({"errorKind": "rate_limit"})["ok"] is True
    assert events[:2] == ["cert", "start"]


def test_tick_skips_while_rotation_in_progress(tmp_path):
    clock = FakeClock()
    credentials = FakeCredentials()
    rotator, state, _, _ = make_rotator(tmp_path, clock=clock, credentials=credentials)
    rotator.lock.acquire()
    try:
        rotator.tick()
    finally:
        rotator.lock.release()
    assert credentials.cert_calls == 0


# Direct/common actions

def test_clear_returns_to_direct_and_drops_flags(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock)
    rotator.rotate({"errorKind": "rate_limit"})
    state.flag_country("NL")
    res = rotator.clear()
    assert res == {"ok": True, "cleared": True, "mode": "direct"}
    assert state.mode == "direct"
    assert state.proxy is None
    assert state.host_ip_flagged_until is None
    assert state.rate_limited_countries == {}
    assert state.rate_limited_servers == {}
    assert state.rate_limited_ips == {}
    assert tunnel.running() is False


def test_startup_forces_direct_mode(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, calls = make_rotator(tmp_path, clock=clock)
    state.mode = "tunnel"
    state.proxy = "http://127.0.0.1:25345"
    state.current_server = "DE#1"
    state.is_rotating = True
    rotator.startup()
    assert state.mode == "direct"
    assert state.proxy is None
    assert state.current_ip == HOST_IP
    assert state.is_rotating is False
    assert tunnel.stops == 0


def test_shutdown_stops_tunnel(tmp_path):
    rotator, state, tunnel, _ = make_rotator(tmp_path)
    rotator.rotate({"errorKind": "rate_limit"})
    rotator.shutdown()
    assert tunnel.running() is False


# State persistence

def test_state_persists_tunnel_fields_in_both_files(tmp_path):
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    state.mode = "tunnel"
    state.proxy = "http://127.0.0.1:25345"
    state.host_ip_flagged_until = 1234.0
    state.current_server = "NL#2"
    state.current_country = "NL"
    state.current_location = "Amsterdam"
    state.current_ip = "200.1.1.2"
    state.last_rotation_time = 999.0
    state.flag_country("DE")
    state.save()

    runtime = json.loads((tmp_path / "runtime.json").read_text())
    assert runtime["mode"] == "tunnel"
    assert runtime["proxy"] == "http://127.0.0.1:25345"
    assert runtime["hostIpFlaggedUntil"] == 1234.0
    assert runtime["server"] == "NL#2"
    assert runtime["egressIp"] == "200.1.1.2"
    assert runtime["rotatedAt"] == 999.0
    for key in ("at", "country", "location", "allRateLimited", "refreshIn",
                "rateLimitedCountries", "rateLimitedCountriesCount", "rotating"):
        assert key in runtime

    reloaded = make_state(tmp_path, clock)
    assert reloaded.mode == "tunnel"
    assert reloaded.proxy == "http://127.0.0.1:25345"
    assert reloaded.host_ip_flagged_until == 1234.0
    assert reloaded.current_server == "NL#2"
    assert "DE" in reloaded.rate_limited_countries


def test_state_drops_expired_flags_on_load(tmp_path):
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    state.rate_limited_servers["NL#1"] = {"at": 100.0, "ttl": 60}
    state.save()
    clock.t = 500.0
    assert make_state(tmp_path, clock).rate_limited_servers == {}


# Service and socket protocol

def run(coro):
    return asyncio.run(coro)


def test_status_reports_direct_and_tunnel(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock)
    svc = service.VpnService(rotator, sock_path=tmp_path / "svc.sock", tick_interval_s=3600)
    direct = run(svc.handle_status())
    assert direct["ok"] is True
    assert direct["connected"] is False
    assert direct["mode"] == "direct"
    assert direct["proxy"] is None
    assert direct["egressIp"] is None
    assert direct["server"] is None
    assert direct["hostIpFlaggedUntil"] is None

    rotator.rotate({"errorKind": "rate_limit"})
    tunnelled = run(svc.handle_status())
    assert tunnelled["connected"] is True
    assert tunnelled["mode"] == "tunnel"
    assert tunnelled["proxy"] == "http://127.0.0.1:25345"
    assert tunnelled["server"] == "DE#1"
    assert tunnelled["country"] == "DE"
    assert tunnelled["egressIp"].startswith("200.")


def test_handle_rotate_sets_rotating_flag_while_running(tmp_path):
    seen = []
    rotator, state, _, _ = make_rotator(tmp_path)
    rotator.probe = lambda env: seen.append(state.is_rotating) or "200"
    svc = service.VpnService(rotator, sock_path=tmp_path / "svc.sock", tick_interval_s=3600)
    res = run(svc.handle_rotate({"action": "rotate", "session": "s1", "errorKind": "rate_limit"}))
    assert res["ok"] is True
    assert seen == [True]
    assert state.is_rotating is False


def test_debounce_only_applies_while_tunnelling(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock)
    svc = service.VpnService(rotator, sock_path=tmp_path / "svc.sock", tick_interval_s=3600)
    first = run(svc.handle_rotate({"session": "s1", "errorKind": "rate_limit"}))
    assert first["ok"] is True and not first.get("debounced")
    second = run(svc.handle_rotate({"session": "s1", "errorKind": "rate_limit"}))
    assert second["debounced"] is True
    assert len(tunnel.starts) == 1
    assert state.debounced_sessions == ["s1"]
    third = run(svc.handle_rotate({"session": "s2", "errorKind": "rate_limit"}))
    assert third["debounced"] is True
    assert len(tunnel.starts) == 1
    clock.t += service.DEBOUNCE_S
    fourth = run(svc.handle_rotate({"session": "s2", "errorKind": "rate_limit"}))
    assert fourth["ok"] is True and not fourth.get("debounced")
    assert len(tunnel.starts) == 2


def test_socket_roundtrip(tmp_path):
    clock = FakeClock()
    rotator, state, tunnel, _ = make_rotator(tmp_path, clock=clock)
    sock = str(tmp_path / "vpn-rotate.sock")

    async def scenario():
        svc = service.VpnService(rotator, sock_path=tmp_path / "vpn-rotate.sock", tick_interval_s=3600)
        await svc.start()
        try:
            status = await asyncio.to_thread(client.send_request, {"action": "status"}, sock)
            rotate = await asyncio.to_thread(
                client.send_request, {"action": "rotate", "session": "s1", "errorKind": "rate_limit"}, sock
            )
            unknown = await asyncio.to_thread(client.send_request, {"action": "bogus"}, sock)
            cleared = await asyncio.to_thread(client.send_request, {"action": "clear"}, sock)
            return status, rotate, unknown, cleared
        finally:
            await svc.stop()

    status, rotate, unknown, cleared = run(scenario())
    assert status["ok"] is True and status["mode"] == "direct"
    assert rotate["ok"] is True and rotate["mode"] == "tunnel"
    assert unknown["ok"] is False and "unknown action" in unknown["reason"]
    assert cleared["cleared"] is True
    assert not os.path.exists(sock)


# Wireproxy tunnel

class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.stdout = io.StringIO("")
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if (self.terminated or self.killed) else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class ProcRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        proc = FakeProc(argv)
        self.calls.append(proc)
        return proc


def make_tunnel(tmp_path, spawn=None, ready=True, log=None):
    return service.WireproxyTunnel(
        wireproxy_bin="/nix/store/fake-wireproxy",
        port=25345,
        runtime_dir=tmp_path,
        log=log,
        spawn=spawn or ProcRecorder(),
        wait_port=lambda port, timeout: ready,
    )


def test_tunnel_writes_private_config_and_cleans_up(tmp_path):
    spawn = ProcRecorder()
    tunnel = make_tunnel(tmp_path, spawn=spawn)
    entry = {"server": "NL#2", "entry_ip": "190.2.132.2", "public_key": "PUBNL#2"}
    assert tunnel.start(KEY, entry) is True
    conf = spawn.calls[0].argv[2]
    assert spawn.calls[0].argv[1:] == ["-c", conf, "-s"]
    body = open(conf).read()
    assert f"PrivateKey = {KEY}" in body
    assert "Endpoint = 190.2.132.2:51820" in body
    assert "BindAddress = 127.0.0.1:25345" in body
    assert "MTU = 1280" in body
    assert "AllowedIPs = 0.0.0.0/0" in body
    assert stat.S_IMODE(os.stat(conf).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(conf)).st_mode) == 0o700
    assert tunnel.running() is True
    assert tunnel.proxy_url == "http://127.0.0.1:25345"
    tunnel.stop()
    assert not os.path.exists(conf)
    assert spawn.calls[0].terminated is True
    assert tunnel.running() is False
    assert tunnel.proxy_url is None


def test_tunnel_never_logs_the_private_key(tmp_path, capsys):
    log = service.Log(tmp_path / "service.log")
    tunnel = make_tunnel(tmp_path, ready=False, log=log)
    entry = {"server": "NL#2", "entry_ip": "190.2.132.2", "public_key": "PUBNL#2"}
    assert tunnel.start(KEY, entry) is False
    tunnel.stop()
    captured = capsys.readouterr()
    assert KEY not in captured.err
    assert KEY not in (tmp_path / "service.log").read_text()
    assert "did not open 127.0.0.1:25345" in captured.err


def test_tunnel_start_failure_when_port_never_opens(tmp_path):
    spawn = ProcRecorder()
    tunnel = make_tunnel(tmp_path, spawn=spawn, ready=False)
    entry = {"server": "NL#2", "entry_ip": "190.2.132.2", "public_key": "PUBNL#2"}
    assert tunnel.start(KEY, entry) is False
    assert tunnel.running() is False
    assert tunnel.conf_path is None
    assert spawn.calls[0].terminated is True


def test_tunnel_start_requires_key(tmp_path):
    spawn = ProcRecorder()
    tunnel = make_tunnel(tmp_path, spawn=spawn)
    assert tunnel.start(None, {"server": "NL#2", "entry_ip": "1.1.1.1", "public_key": "PUB"}) is False
    assert spawn.calls == []


# Credentials

# Session and credentials

def test_proton_session_caches_session_and_maps_server_list(tmp_path):
    clock = FakeClock()

    class FakeVPNSession:
        def __init__(self):
            self.fetches = 0

        async def fetch_server_list(self):
            self.fetches += 1
            return FakeServerListResult(expiration=clock.now() + 60)

    session = FakeVPNSession()
    created = []
    proton = service.ProtonSession(session_factory=lambda: (created.append(1), session)[1])
    assert proton.fetch_server_list()["LogicalServers"] == DEFAULT_LOGICALS
    assert proton.get() is session
    assert created == [1]
    assert session.fetches == 1


class FakePubkeyCredentials:
    def __init__(self, remaining):
        self.certificate_validity_remaining = remaining
        self.wg_private_key = KEY


class FakeSession:
    def __init__(self, remaining=3600.0, error=None):
        self.pubkey = FakePubkeyCredentials(remaining)
        self.fetches = 0
        self.error = error
        self.vpn_account = types.SimpleNamespace(
            vpn_credentials=types.SimpleNamespace(pubkey_credentials=self.pubkey)
        )

    async def fetch_certificate(self):
        self.fetches += 1
        if self.error:
            raise self.error
        self.pubkey.certificate_validity_remaining = 7 * 86400


def test_credentials_reads_key_and_refreshes_soon_to_expire_certificate(tmp_path):
    clock = FakeClock()
    session = FakeSession(remaining=3600.0)
    creds = service.ProtonCredentials(session_factory=lambda: session, now_fn=clock.now)
    assert creds.wg_private_key() == KEY
    assert creds.ensure_certificate() is True
    assert session.fetches == 1
    assert creds.ensure_certificate() is True
    assert session.fetches == 1


def test_credentials_throttles_failed_refresh(tmp_path):
    clock = FakeClock()
    session = FakeSession(remaining=60.0, error=RuntimeError("offline"))
    creds = service.ProtonCredentials(session_factory=lambda: session, now_fn=clock.now, retry_s=600)
    assert creds.ensure_certificate() is False
    assert session.fetches == 1
    clock.t += 100
    assert creds.ensure_certificate() is False
    assert session.fetches == 1
    clock.t += 700
    creds.ensure_certificate()
    assert session.fetches == 2


def test_credentials_reports_missing_session_without_raising(tmp_path):
    def boom():
        raise RuntimeError("no keyring")

    creds = service.ProtonCredentials(session_factory=boom)
    assert creds.wg_private_key() is None
    assert creds.ensure_certificate() is False


# Helpers and client output

def test_fetch_egress_ip_validates_output(monkeypatch):
    monkeypatch.setattr(service, "capture", lambda argv, timeout, env=None: (0, "1.2.3.4\n"))
    assert service.fetch_egress_ip("/bin/sh") == "1.2.3.4"
    monkeypatch.setattr(service, "capture", lambda argv, timeout, env=None: (0, "<html>oops</html>"))
    assert service.fetch_egress_ip("/bin/sh") is None
    monkeypatch.setattr(service, "capture", lambda argv, timeout, env=None: (1, "1.2.3.4"))
    assert service.fetch_egress_ip("/bin/sh") is None
    assert service.fetch_egress_ip("/nonexistent/bun") is None


def test_fetch_egress_ip_passes_proxy_env(monkeypatch):
    seen = {}

    def fake(argv, timeout, env=None):
        seen.update(env or {})
        return 0, "1.2.3.4"

    monkeypatch.setattr(service, "capture", fake)
    service.fetch_egress_ip("/bin/sh", {"HTTPS_PROXY": "http://127.0.0.1:25345"})
    assert seen["HTTPS_PROXY"] == "http://127.0.0.1:25345"


def test_base_country():
    assert service.base_country("US-FREE#1") == "US"
    assert service.base_country("NL#369") == "NL"


def test_client_prints_direct_mode(capsys):
    client.print_status({"ok": True, "connected": False, "mode": "direct", "egressIp": "100.0.0.1"})
    out = capsys.readouterr().out
    assert "Direct" in out
    assert "100.0.0.1" in out


def test_client_prints_tunnel_mode(capsys):
    client.print_status({
        "ok": True,
        "connected": True,
        "mode": "tunnel",
        "proxy": "http://127.0.0.1:25345",
        "server": "NL#2",
        "country": "NL",
        "location": "Amsterdam",
        "egressIp": "200.1.1.2",
    })
    out = capsys.readouterr().out
    assert "NL#2" in out
    assert "200.1.1.2" in out


def test_client_reports_errors(capsys):
    client.print_status({"ok": False, "reason": "boom"})
    assert "boom" in capsys.readouterr().err


def test_opencode_api_key_order(tmp_path):
    import sqlite3

    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE credential (integration_id text, value text, active integer)")
    con.execute("INSERT INTO credential VALUES ('opencode', '{\"type\":\"key\",\"key\":\"sk-db\"}', 1)")
    con.commit()
    con.close()
    keyfile = tmp_path / "key"
    keyfile.write_text("sk-file\n")
    env = {"OPENCODE_DB": str(db), "PI_OPENCODE_FREE_API_KEY_FILE": str(keyfile)}
    assert service.opencode_api_key({**env, "PI_OPENCODE_FREE_API_KEY": "sk-env"}) == "sk-env"
    assert service.opencode_api_key(env) == "sk-file"
    assert service.opencode_api_key({"OPENCODE_DB": str(db)}) == "sk-db"
    assert service.opencode_api_key({"OPENCODE_DB": str(tmp_path / "none.db")}) == "public"
