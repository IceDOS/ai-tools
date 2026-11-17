"""Tests for pi-agent VPN rotation service and client."""
import asyncio
import io
import json
import socket
import sys

import pi_vpn_rotate as client
import pi_vpn_service as service

COUNTRIES_OUTPUT = """Country                           Code
--------------------------------  ------
Albania                           AL
Austria                           AT
Belgium                           BE
United States                     US
Greece                            GR
"""

STATUS_CONNECTED_AT = "Status: Connected\nServer: AT#12 in Vienna\n"
STATUS_CONNECTED_US = "Status: Connected\nServer: US-FREE#1 in New York\n"
STATUS_DISCONNECTED = "Status: Disconnected\n"
CONNECT_OK_BE = "Connected to BE#3 in Brussels.\nYour new IP address is 1.2.3.4.\n"
CONNECT_OK_US_1 = "Connected to US-FREE#1 in New York.\nYour new IP address is 10.0.0.1.\n"
CONNECT_OK_US_2 = "Connected to US-FREE#2 in Chicago.\nYour new IP address is 10.0.0.2.\n"


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def make_test_state(tmp_path, clock=None):
    clock = clock or FakeClock()
    state_file = tmp_path / "state.json"
    runtime_file = tmp_path / "runtime.json"
    return service.VpnState(state_file, runtime_file, now_fn=clock.now)


def make_test_rotator(tmp_path, clock=None, replies=None, calls=None, **kwargs):
    clock = clock or FakeClock()
    calls = calls if calls is not None else []
    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)

    default_replies = {
        "status": (0, STATUS_CONNECTED_AT),
        "countries": (0, COUNTRIES_OUTPUT),
        "connect": (0, CONNECT_OK_BE),
    }
    if replies:
        default_replies.update(replies)

    def run_fn(argv, timeout):
        calls.append(list(argv))
        cmd = argv[1]
        if cmd in default_replies:
            return default_replies[cmd]
        return 0, ""

    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        has_iface=lambda path: False,
        get_ip=lambda: "1.2.3.4",
        **kwargs,
    )
    return rotator, state, calls, clock


# 1. Parsing and helper tests

def test_parse_countries_skips_header():
    countries = service.parse_countries(COUNTRIES_OUTPUT)
    assert countries == ["AL", "AT", "BE", "US", "GR"]


def test_parse_status():
    assert service.parse_status(STATUS_CONNECTED_AT) == ("AT#12", "Vienna")
    assert service.parse_status(STATUS_DISCONNECTED) is None


def test_base_country():
    assert service.base_country("US-FREE#1") == "US"
    assert service.base_country("GR#14") == "GR"
    assert service.base_country("DE-1") == "DE"
    assert service.base_country("") == ""


def test_next_country_cycling():
    pool = ["AT", "BE", "US"]
    assert service.next_country(pool, "AT") == "BE"
    assert service.next_country(pool, "BE") == "US"
    assert service.next_country(pool, "US") == "AT"


def test_next_country_single_country_mode():
    assert service.next_country(["US"], "US") == "US"
    assert service.next_country(["GR"], None) == "GR"


def test_log_keeps_last_lines(tmp_path):
    log_path = tmp_path / "test.log"
    log = service.Log(log_path)
    for i in range(service.LOG_LINES + 50):
        log(f"entry {i}")
    lines = log_path.read_text().splitlines()
    assert len(lines) == service.LOG_LINES
    assert "entry 249" in lines[-1]


# 2. VpnState tests

def test_state_load_save_persistence(tmp_path):
    clock = FakeClock(2000.0)
    state = make_test_state(tmp_path, clock)
    state.flag_country("US", ttl=3600)
    state.flag_server("US#1", ip="1.1.1.1", ttl=1800)
    state.last_attempt_at = 1950.0
    state.rotations = [1900.0, 1950.0]
    state.save()

    state2 = make_test_state(tmp_path, clock)
    assert state2.is_country_rate_limited("US")
    assert state2.is_server_rate_limited("US#1")
    assert state2.is_server_rate_limited("OTHER", ip="1.1.1.1")
    assert state2.last_attempt_at == 1950.0
    assert len(state2.rotations) == 2


def test_state_clean_expired(tmp_path):
    clock = FakeClock(1000.0)
    state = make_test_state(tmp_path, clock)
    state.flag_country("US", ttl=100)
    state.flag_server("US#1", ip="1.1.1.1", ttl=50)

    assert state.is_country_rate_limited("US")
    assert state.is_server_rate_limited("US#1")

    clock.sleep(60)  # t=1060
    assert state.is_country_rate_limited("US")
    assert not state.is_server_rate_limited("US#1")

    clock.sleep(50)  # t=1110
    assert not state.is_country_rate_limited("US")


def test_state_clear(tmp_path):
    clock = FakeClock(1000.0)
    state = make_test_state(tmp_path, clock)
    state.flag_country("US")
    state.flag_server("US#1")
    state.rotations = [1000.0]
    state.all_rate_limited = True
    assert state.is_country_rate_limited("US")

    state.clear()
    assert not state.is_country_rate_limited("US")
    assert not state.is_server_rate_limited("US#1")
    assert state.rotations == []
    assert not state.all_rate_limited


# 3. VpnRotator tests

def test_rotate_success(tmp_path):
    rotator, state, calls, clock = make_test_rotator(tmp_path)
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["server"] == "BE#3"
    assert res["country"] == "BE"
    assert state.current_server == "BE#3"
    assert state.current_country == "BE"
    assert len(state.rotations) == 1


def test_account_limit_skips_rotation(tmp_path):
    rotator, state, calls, clock = make_test_rotator(tmp_path)
    res = rotator.rotate({"errorMessage": "GoUsageLimitError: Monthly usage limit reached"})
    assert res["ok"] is False
    assert res["reason"] == "account limit"
    assert len(calls) == 0


def test_cooldown_blocks_initial_attempt_but_allows_retry(tmp_path):
    rotator, state, calls, clock = make_test_rotator(tmp_path, cooldown_s=600)
    # First rotation at t=1000
    res1 = rotator.rotate({"errorKind": "rate_limit", "retry": 0})
    assert res1["ok"] is True

    # Advance clock by 100s (within 600s cooldown)
    clock.sleep(100)
    # Fresh prompt failure (retry=0) is blocked by cooldown
    res2 = rotator.rotate({"errorKind": "rate_limit", "retry": 0})
    assert res2["ok"] is False
    assert res2["reason"] == "cool-down"

    # In-progress retry (retry=1) is allowed through cooldown
    res3 = rotator.rotate({"errorKind": "rate_limit", "retry": 1})
    assert res3["ok"] is True


def test_hourly_cap_blocks_rotation(tmp_path):
    rotator, state, calls, clock = make_test_rotator(tmp_path, max_per_hour=2, cooldown_s=0)
    res1 = rotator.rotate({"errorKind": "rate_limit"})
    assert res1["ok"] is True

    clock.sleep(60)
    res2 = rotator.rotate({"errorKind": "rate_limit"})
    assert res2["ok"] is True

    clock.sleep(60)
    res3 = rotator.rotate({"errorKind": "rate_limit"})
    assert res3["ok"] is False
    assert res3["reason"] == "hourly cap"


def test_zero_hourly_cap_and_cooldown_disable_limits(tmp_path):
    rotator, state, calls, clock = make_test_rotator(tmp_path, max_per_hour=0, cooldown_s=0)
    for _ in range(5):
        res = rotator.rotate({"errorKind": "rate_limit"})
        assert res["ok"] is True
        clock.sleep(1)


def test_allowlist_and_blocklist_filtering(tmp_path):
    replies = {
        "status": (0, STATUS_CONNECTED_AT),
        "countries": (0, COUNTRIES_OUTPUT),
        "connect": (0, CONNECT_OK_BE),
    }
    rotator, state, calls, clock = make_test_rotator(
        tmp_path,
        replies=replies,
        countries_allowlist=["BE", "GR"],
        countries_blocklist=["GR"],
    )
    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["country"] == "BE"
    connect_calls = [c for c in calls if c[1] == "connect"]
    for call in connect_calls:
        if "--country" in call:
            idx = call.index("--country")
            assert call[idx + 1] != "GR"


def test_single_country_mode_reconnects_and_tries_multiple_servers(tmp_path):
    clock = FakeClock(1000.0)
    calls = []
    # US is currently connected
    replies = {
        "status": (0, STATUS_CONNECTED_US),
        "countries": (0, COUNTRIES_OUTPUT),
    }
    connect_attempts = 0

    def run_fn(argv, timeout):
        nonlocal connect_attempts
        calls.append(list(argv))
        cmd = argv[1]
        if cmd == "connect":
            connect_attempts += 1
            if connect_attempts == 1:
                # First attempt lands on US-FREE#1 (which was the rate-limited server)
                return 0, CONNECT_OK_US_1
            # Second attempt lands on US-FREE#2 (a different server with clean IP)
            return 0, CONNECT_OK_US_2
        return replies.get(cmd, (0, ""))

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        countries_allowlist=["US"],
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit", "retry": 0})
    assert res["ok"] is True
    assert res["server"] == "US-FREE#2"
    assert connect_attempts == 2


def test_landing_on_flagged_server_does_not_flag_whole_country(tmp_path):
    clock = FakeClock(1000.0)
    calls = []
    connect_attempts = 0

    def run_fn(argv, timeout):
        nonlocal connect_attempts
        calls.append(list(argv))
        cmd = argv[1]
        if cmd == "connect":
            connect_attempts += 1
            if connect_attempts == 1:
                return 0, CONNECT_OK_US_1
            return 0, CONNECT_OK_US_2
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    # Pre-flag US-FREE#1
    state.flag_server("US-FREE#1", ip="10.0.0.1", country="US")

    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        countries_allowlist=["US"],
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["server"] == "US-FREE#2"
    # Country US must NOT be rate limited!
    assert not state.is_country_rate_limited("US")


def test_time_budget_exhaustion(tmp_path):
    clock = FakeClock(1000.0)
    calls = []

    def run_fn(argv, timeout):
        calls.append(list(argv))
        cmd = argv[1]
        if cmd == "connect":
            # Simulate a slow connect taking 60s
            clock.sleep(60.0)
            return 1, "connect timed out"
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        rotate_budget_s=100.0,
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is False
    # Budget was 100s, connect took 60s + costs, so second attempt broke out
    connect_calls = [c for c in calls if c[1] == "connect"]
    assert len(connect_calls) < 4


def test_all_rate_limited_exhaustion(tmp_path):
    clock = FakeClock(1000.0)
    calls = []

    def run_fn(argv, timeout):
        calls.append(list(argv))
        cmd = argv[1]
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        if cmd == "connect":
            return 0, CONNECT_OK_BE
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)

    # Mock probe that always returns 429
    def probe_429():
        return "429"

    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        probe=probe_429,
        countries_allowlist=["BE"],
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit", "provider": "opencode-free"})
    assert res["ok"] is False
    assert res["reason"] == "all_rate_limited"
    assert state.all_rate_limited is True
    assert state.is_country_rate_limited("BE")


def test_get_live_status_reporting(tmp_path):
    clock = FakeClock(1000.0)
    current_status = STATUS_CONNECTED_AT

    def run_fn(argv, timeout):
        cmd = argv[1]
        if cmd == "status":
            return 0, current_status
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    status = rotator.get_live_status()
    assert status["connected"] is True
    assert status["server"] == "AT#12"

    # Simulate manual disconnect or reboot
    current_status = STATUS_DISCONNECTED
    status2 = rotator.get_live_status()
    assert status2["connected"] is False
    assert status2["server"] is None


# 4. VpnService tests (async socket IPC)

def test_service_socket_communication(tmp_path):
    async def run_test():
        clock = FakeClock(1000.0)
        rotator, state, calls, clock = make_test_rotator(tmp_path, clock=clock, cooldown_s=0)
        sock_path = tmp_path / "test.sock"
        srv = service.VpnService(rotator, sock_path=sock_path)
        await srv.start()

        try:
            # Test status command
            resp = await asyncio.to_thread(client.send_request, {"action": "status"}, sock_path=sock_path)
            assert resp["ok"] is True
            assert resp["connected"] is True
            assert resp["server"] == "AT#12"

            # Test rotate command
            resp2 = await asyncio.to_thread(client.send_request, {"action": "rotate", "errorKind": "rate_limit"}, sock_path=sock_path)
            assert resp2["ok"] is True
            assert resp2["server"] == "BE#3"

            # Test clear command
            resp3 = await asyncio.to_thread(client.send_request, {"action": "clear"}, sock_path=sock_path)
            assert resp3["ok"] is True
            assert resp3["cleared"] is True
        finally:
            await srv.stop()

    asyncio.run(run_test())


def test_service_debouncing_concurrent_clients(tmp_path):
    async def run_test():
        clock = FakeClock(1000.0)
        rotator, state, calls, clock = make_test_rotator(tmp_path, clock=clock, cooldown_s=0)
        sock_path = tmp_path / "test.sock"
        srv = service.VpnService(rotator, sock_path=sock_path)
        await srv.start()

        try:
            # First client rotates
            resp1 = await asyncio.to_thread(client.send_request, {"action": "rotate", "session": "ses_A"}, sock_path=sock_path)
            assert resp1["ok"] is True
            assert resp1.get("debounced") is not True

            # Second client requests rotation within 10s debounce window
            clock.sleep(2.0)
            resp2 = await asyncio.to_thread(client.send_request, {"action": "rotate", "session": "ses_B"}, sock_path=sock_path)
            assert resp2["ok"] is True
            assert resp2.get("debounced") is True
            assert resp2["server"] == resp1["server"]
        finally:
            await srv.stop()

    asyncio.run(run_test())


def test_handle_rotate_rotating_flag(tmp_path):
    async def run_test():
        clock = FakeClock(1000.0)
        rotator, state, calls, clock = make_test_rotator(tmp_path, clock=clock, cooldown_s=0)
        srv = service.VpnService(rotator, sock_path=tmp_path / "test.sock")
        seen = []

        def observe(req):
            seen.append(json.loads(state.runtime_file.read_text())["rotating"])
            return rotate(req)

        rotate, rotator.rotate = rotator.rotate, observe
        res = await srv.handle_rotate({"session": "ses_A"})
        assert res["ok"] is True
        assert seen == [True], "runtime file must say rotating while rotate() runs"
        assert json.loads(state.runtime_file.read_text())["rotating"] is False
        assert (await srv.handle_status())["rotating"] is False

        # only a finished rotation moves rotatedAt; every other save moves at
        rotated_at = json.loads(state.runtime_file.read_text())["rotatedAt"]
        assert rotated_at == state.last_rotation_time
        clock.sleep(5)
        state.save()
        runtime = json.loads(state.runtime_file.read_text())
        assert runtime["rotatedAt"] == rotated_at and runtime["at"] > rotated_at * 1000

        clock.sleep(service.DEBOUNCE_S + 1)  # past the debounce window so rotate() runs again

        def boom(req):
            seen.append(json.loads(state.runtime_file.read_text())["rotating"])
            raise RuntimeError("service killed mid-rotation")

        rotator.rotate = boom
        try:
            await srv.handle_rotate({"session": "ses_B"})
        except RuntimeError:
            pass
        else:
            raise AssertionError("rotate() errors must propagate")
        assert seen == [True, True]
        assert json.loads(state.runtime_file.read_text())["rotating"] is False

    asyncio.run(run_test())


# 5. Client CLI tests

def test_client_main_status_does_not_read_stdin(monkeypatch):
    # If stdin.read() were called, reading closed StringIO would raise EOF or empty,
    # but more importantly sys.stdin is set to raise an error if touched.
    class ExplosiveStdin:
        def read(self):
            raise AssertionError("stdin.read() must not be called for status!")

        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", ExplosiveStdin())
    monkeypatch.setattr(client, "send_request", lambda req, **kw: {"ok": True, "connected": True, "server": "TEST#1"})

    ret = client.main(["status"])
    assert ret == 0


def test_client_main_clear_does_not_read_stdin(monkeypatch):
    class ExplosiveStdin:
        def read(self):
            raise AssertionError("stdin.read() must not be called for clear!")

        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", ExplosiveStdin())
    monkeypatch.setattr(client, "send_request", lambda req, **kw: {"ok": True, "cleared": True})

    ret = client.main(["clear"])
    assert ret == 0


def test_client_main_hook_stdin_handling(monkeypatch):
    payload = json.dumps({"session": "s1", "retry": 1, "errorMessage": "rate limit"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    sent = []

    def mock_send(req, **kw):
        sent.append(req)
        return {"ok": True, "server": "BE#3", "country": "BE"}

    monkeypatch.setattr(client, "send_request", mock_send)

    ret = client.main([])
    assert ret == 0
    assert len(sent) == 1
    assert sent[0]["action"] == "rotate"
    assert sent[0]["session"] == "s1"
    assert sent[0]["retry"] == 1


def test_client_main_failure_exits_one(monkeypatch):
    monkeypatch.setattr(client, "send_request", lambda req, **kw: {"ok": False, "reason": "all_rate_limited"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    ret = client.main([])
    assert ret == 1


def test_probe_script_syntax_valid(tmp_path):
    # Verify that PROBE_SCRIPT is syntactically valid JavaScript
    script = service.PROBE_SCRIPT.replace("__PROBE_MODEL__", json.dumps("test-model"))
    f = tmp_path / "probe.js"
    f.write_text(script)
    code, out = service.capture(["node", "-c", str(f)], timeout=5)
    assert code == 0, f"Probe script has syntax errors: {out}"


def test_free_plan_plain_connect_fallback(tmp_path):
    clock = FakeClock(1000.0)
    calls = []

    def run_fn(argv, timeout):
        calls.append(list(argv))
        cmd = argv[1]
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        if cmd == "connect":
            if "--country" in argv:
                return 1, f"Selecting a specific server or country {service.FREE_PLAN_TEXT}."
            return 0, "Connected to NL#10 in Amsterdam.\n"
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit"})
    assert res["ok"] is True
    assert res["server"] == "NL#10"
    assert res["country"] == "NL"


def test_failed_rotation_does_not_start_cooldown(tmp_path):
    clock = FakeClock(1000.0)

    def run_fn(argv, timeout):
        cmd = argv[1]
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        if cmd == "connect":
            return 1, "connect failed"
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        countries_allowlist=["BE"],
        cooldown_s=600,
    )

    res = rotator.rotate({"errorKind": "rate_limit", "retry": 0})
    assert res["ok"] is False
    # last_attempt_at must NOT be set on failed rotation
    assert state.last_attempt_at == 0


def test_network_failure_uses_unreachable_reason_and_short_ttl(tmp_path):
    clock = FakeClock(1000.0)

    def run_fn(argv, timeout):
        cmd = argv[1]
        if cmd == "status":
            return 0, STATUS_CONNECTED_AT
        if cmd == "countries":
            return 0, COUNTRIES_OUTPUT
        if cmd == "connect":
            return 1, "connection timeout"
        return 0, ""

    state = make_test_state(tmp_path, clock)
    log = service.Log(tmp_path / "test.log", now_fn=clock.now)
    rotator = service.VpnRotator(
        state=state,
        log=log,
        run_fn=run_fn,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        countries_allowlist=["BE"],
        cooldown_s=0,
    )

    res = rotator.rotate({"errorKind": "rate_limit", "retry": 0})
    assert res["ok"] is False
    assert state.is_country_rate_limited("BE")
    entry = state.rate_limited_countries["BE"]
    assert entry["reason"] == "unreachable"
    assert entry["ttl"] == service.DEFAULT_UNREACHABLE_TTL_S


def test_client_recovers_from_stale_socket(tmp_path, monkeypatch):
    stale_sock = tmp_path / "stale.sock"
    stale_sock.write_text("abandoned")
    restarted = []

    def mock_ensure(p):
        restarted.append(p)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(p))
        s.listen(1)
        mock_ensure.sock = s
        return True

    monkeypatch.setattr(client, "ensure_service_running", mock_ensure)
    monkeypatch.setattr(socket.socket, "recv", lambda self, bufsize: b'{"ok": true}\n')

    try:
        resp = client.send_request({"action": "status"}, sock_path=stale_sock)
        assert resp["ok"] is True
        assert len(restarted) == 1
    finally:
        if hasattr(mock_ensure, "sock"):
            mock_ensure.sock.close()
