import fcntl
import io
import json
import signal

import pytest

import pi_vpn_rotate as vpn

COUNTRIES = "Country                           Code\n--------------------------------  ------\nAlbania                           AL\nAustria                           AT\nBelgium                           BE\n"
CONNECTED = "Status: Connected\nServer: AT#12 in Vienna\n"
CONNECT_OK = "Connected to BE#3 in Brussels.\nYour new IP address is 1.2.3.4.\n"


class Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def fake_run(replies, calls):
    def run(argv, timeout):
        calls.append(argv[1:])
        return replies[argv[1]]
    return run


def ok_run(calls):
    return fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (0, CONNECT_OK)}, calls)


def main(tmp_path, clock, run, payload="{}", has_iface=None, get_ip=lambda: None, **env):
    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_PROBE": "0", **env}
    kwargs = {"has_iface": has_iface} if has_iface is not None else {}
    return vpn.main([], environ, io.StringIO(payload), run, clock.now, clock.sleep, get_ip=get_ip, **kwargs)


def state(tmp_path):
    return json.loads((tmp_path / "pi-vpn-rotate.json").read_text())


def seed(tmp_path, record):
    (tmp_path / "pi-vpn-rotate.json").write_text(json.dumps(record))


def is_locked(path):
    try:
        f = open(path, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        return False
    except BlockingIOError:
        return True


def test_parse_countries_skips_the_header():
    assert vpn.parse_countries(COUNTRIES) == ["AL", "AT", "BE"]


def test_parse_status():
    assert vpn.parse_status(CONNECTED) == ("AT#12", "Vienna")
    assert vpn.parse_status("Status: Disconnected\n") is None


def test_next_country_cycles():
    assert vpn.next_country(["AL", "AT", "BE"], "AT") == "BE"
    assert vpn.next_country(["AL", "AT", "BE"], "BE") == "AL"
    assert vpn.next_country(["AL", "AT"], None) == "AL"
    assert vpn.next_country([], "AT") is None


def test_rotation_connects_to_the_next_country_and_records_it(tmp_path):
    calls = []
    assert main(tmp_path, Clock(), ok_run(calls), '{"provider": "opencode", "errorKind": "rate_limit"}') == 0
    assert ["connect", "--country", "BE", "--random"] in calls
    assert state(tmp_path) == {
        "at": 1_000_000, "attemptAt": 1_000_000, "rotations": [1_000_000], "country": "BE",
        "server": "BE#3", "location": "Brussels", "egressIp": "1.2.3.4",
        "flagged": {
            "AT#12": {"at": 1000.0, "reason": "rate_limit", "ttl": 3600},
        },
    }
    assert not is_locked(tmp_path / "pi-vpn-rotate.lock")
    assert "opencode rate_limit: rotated" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_an_account_limit_never_rotates(tmp_path):
    calls = []
    payload = json.dumps({"provider": "opencode", "errorMessage": "GoUsageLimitError: limit reached"})
    assert main(tmp_path, Clock(), ok_run(calls), payload) == 1
    assert calls == []
    assert "account limit" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_a_rotation_by_another_session_seconds_ago_is_adopted(tmp_path):
    seed(tmp_path, {"at": 995_000, "rotations": [995_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(), ok_run(calls)) == 0
    assert calls == []
    assert "debounced" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_the_cool_down_blocks_a_rotation(tmp_path):
    seed(tmp_path, {"at": 940_000, "rotations": [940_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(), ok_run(calls)) == 1
    assert calls == []
    assert "cool-down" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_the_cool_down_comes_from_the_environment(tmp_path):
    seed(tmp_path, {"at": 940_000, "rotations": [940_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(), ok_run(calls), PI_VPN_ROTATE_COOLDOWN_S="30") == 0
    assert ["connect", "--country", "BE", "--random"] in calls


def test_the_hourly_cap_blocks_a_rotation(tmp_path):
    seed(tmp_path, {"at": 9_000_000, "rotations": [7_000_000, 8_000_000, 9_000_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(10_000.0), ok_run(calls)) == 1
    assert calls == []
    assert "hourly cap" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_rotations_older_than_an_hour_are_dropped(tmp_path):
    seed(tmp_path, {"at": 8_000_000, "rotations": [5_000_000, 7_000_000, 8_000_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(10_000.0), ok_run(calls)) == 0
    assert state(tmp_path)["rotations"] == [7_000_000, 8_000_000, 10_000_000]


def test_disconnected_vpn_connects_to_new_server(tmp_path):
    calls = []
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, "Status: Disconnected\n"), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run, has_iface=lambda _: False) == 0
    assert calls == [["status"], ["countries", "list"], ["connect", "--country", "AL", "--random"]]
    assert state(tmp_path)["server"] == "BE#3"
    assert "VPN is disconnected; connecting to new server" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_disconnected_vpn_disconnects_if_all_attempts_fail(tmp_path):
    calls = []
    run = fake_run({
        "countries": (0, COUNTRIES),
        "status": (0, "Status: Disconnected\n"),
        "connect": (1, "Connection failed\n"),
        "disconnect": (0, "Disconnected\n"),
    }, calls)
    assert main(tmp_path, Clock(), run, has_iface=lambda _: False) == 1
    assert calls == [["status"], ["countries", "list"], ["connect", "--country", "AL", "--random"], ["disconnect"]]
    assert "all connection attempts failed; disconnecting" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_disconnected_status_with_proton0_interface_is_logged_distinctly(tmp_path):
    calls = []
    run = fake_run({"status": (0, "Status: Disconnected\n")}, calls)
    assert main(tmp_path, Clock(), run, has_iface=lambda _: True) == 1
    assert "VPN status is disconnected but proton0 interface is up; skipping rotation" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_status_command_failure_is_logged_distinctly(tmp_path):
    calls = []
    run = fake_run({"status": (1, "timeout"), "countries": (0, COUNTRIES)}, calls)
    assert main(tmp_path, Clock(), run) == 1
    assert "status check failed: timeout" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_a_failed_connect_exits_non_zero(tmp_path):
    calls = []
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (1, "no route")}, calls)
    assert main(tmp_path, Clock(), run) == 1
    assert ["connect", "--country", "BE", "--random"] in calls
    assert "rotation failed" in (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "at" not in state(tmp_path)
    assert state(tmp_path)["attemptAt"] == 1_000_000
    assert "rotations" not in state(tmp_path)


def test_interrupted_connect_records_attempt_before_running(tmp_path):
    def run_with_abort(cmd, timeout=0):
        if cmd[1] == "status":
            return 0, CONNECTED
        if cmd[1] == "countries":
            return 0, COUNTRIES
        if cmd[1] == "connect":
            s = state(tmp_path)
            assert s["attemptAt"] == 1_000_000
            assert "rotations" not in s
            raise KeyboardInterrupt()
        return 0, ""

    try:
        main(tmp_path, Clock(), run_with_abort)
    except KeyboardInterrupt:
        pass
    assert state(tmp_path)["attemptAt"] == 1_000_000


def test_failed_rotation_enforces_cooldown(tmp_path):
    calls = []
    clock = Clock()
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (1, "no route")}, calls)
    assert main(tmp_path, clock, run) == 1
    clock.sleep(30)
    # 30s later (under 600s cooldown), a subsequent rate limit is blocked by cool-down
    assert main(tmp_path, clock, run) == 1
    assert "cool-down" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_failed_rotation_does_not_debounce_subsequent_attempts(tmp_path):
    calls = []
    clock = Clock()
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (1, "no route")}, calls)
    assert main(tmp_path, clock, run) == 1
    clock.sleep(5)
    # 5s later, not debounced because failed rotation did not stamp at
    assert main(tmp_path, clock, run) == 1
    assert "debounced" not in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_failed_attempts_do_not_use_the_hourly_budget(tmp_path):
    calls = []
    clock = Clock()
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (1, "no route")}, calls)
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert "hourly cap" not in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_allowlist_filtering_all_countries_is_logged(tmp_path):
    calls = []
    run = fake_run({"status": (0, CONNECTED), "countries": (0, COUNTRIES)}, calls)
    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COUNTRIES="XX") == 1
    assert "no countries match allowlist" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_countries_allowlist_filters_candidates(tmp_path):
    calls = []
    status = "Status: Connected\nServer: BE#3 in Brussels\n"
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, status), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COUNTRIES="AL") == 0
    assert ["connect", "--country", "AL", "--random"] in calls


def test_server_with_region_suffix_extracts_base_country(tmp_path):
    calls = []
    status = "Status: Connected\nServer: US-NY#12 in New York\n"
    countries = "United States   US\nNetherlands     NL\n"
    run = fake_run({"countries": (0, countries), "status": (0, status), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run) == 0
    assert ["connect", "--country", "NL", "--random"] in calls


def test_the_log_keeps_its_last_lines(tmp_path):
    log = vpn.Log(tmp_path / "pi-vpn-rotate.log", Clock().now)
    for i in range(vpn.LOG_LINES + 5):
        log(f"line {i}")
    lines = (tmp_path / "pi-vpn-rotate.log").read_text().splitlines()
    assert len(lines) == vpn.LOG_LINES
    assert lines[-1].endswith(f"line {vpn.LOG_LINES + 4}")


def test_a_held_lock_gives_up_after_the_wait(tmp_path):
    lock = tmp_path / "pi-vpn-rotate.lock"
    holder = open(lock, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        clock = Clock(1000.0)
        assert vpn.acquire(lock, clock.now, clock.sleep) is None
        assert clock.t - 1000.0 >= vpn.LOCK_WAIT_S
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_lock_is_acquired_when_released(tmp_path):
    lock = tmp_path / "pi-vpn-rotate.lock"
    clock = Clock(1000.0)
    lock_file = vpn.acquire(lock, clock.now, clock.sleep)
    assert lock_file is not None
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()


def test_connect_missing_connected_output_is_treated_as_failure(tmp_path):
    calls = []
    higher_tier = "RequiresHigherTierError: Location selection requires Plus plan"
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (0, higher_tier)}, calls)
    assert main(tmp_path, Clock(), run) == 1
    assert "connect to BE failed" in (tmp_path / "pi-vpn-rotate.log").read_text()
    assert ["connect", "AT#12"] in calls


def test_failed_connect_restores_previous_server(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (1, "timeout")
        if argv[1:] == ["connect", "AT#12"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (1, "unexpected")
    assert main(tmp_path, Clock(), run) == 1
    assert ["connect", "--country", "BE", "--random"] in calls
    assert ["connect", "AT#12"] in calls
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "restoring connection to AT#12" in log_text
    assert "restored connection to AT#12" in log_text


def test_failed_connect_restore_failure_is_logged(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (1, "timeout")
        if argv[1:] == ["connect", "AT#12"]:
            return (1, "restore timeout")
        return (1, "unexpected")
    assert main(tmp_path, Clock(), run) == 1
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "restore to AT#12 failed: restore timeout" in log_text


def test_failed_target_is_skipped_on_next_attempt(tmp_path):
    calls = []
    clock = Clock()
    run = fake_run({
        "countries": (0, COUNTRIES),
        "status": (0, CONNECTED),
        "connect": (1, "BE failed"),
    }, calls)
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert calls[-2] == ["connect", "--country", "BE", "--random"]
    assert calls[-1] == ["connect", "AT#12"]

    # On next rate limit, BE was recorded as failedTarget, so it should try AL
    run2 = fake_run({
        "countries": (0, COUNTRIES),
        "status": (0, CONNECTED),
        "connect": (0, CONNECT_OK),
    }, calls)
    assert main(tmp_path, clock, run2, PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert ["connect", "--country", "AL", "--random"] in calls


def test_single_allowlisted_country_matching_current_exits_one(tmp_path):
    calls = []
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED)}, calls)
    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COUNTRIES="AT") == 1
    assert "matches current (AT)" in (tmp_path / "pi-vpn-rotate.log").read_text()
    assert not any(c[0] == "connect" for c in calls)


def test_egress_ip_uses_get_ip_result(tmp_path):
    calls = []
    assert main(tmp_path, Clock(), ok_run(calls), get_ip=lambda: "203.0.113.42") == 0
    assert state(tmp_path)["egressIp"] == "203.0.113.42"


def test_subsequent_failure_within_debounce_window_rotates_instead_of_debouncing_again(tmp_path):
    seed(tmp_path, {"at": 995_000, "rotations": [995_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(1000.0), ok_run(calls), PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert calls == []
    assert "debounced" in (tmp_path / "pi-vpn-rotate.log").read_text()

    calls2 = []
    assert main(tmp_path, Clock(1002.0), ok_run(calls2), PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert ["connect", "--country", "BE", "--random"] in calls2


def test_probe_failure_advances_to_next_country(tmp_path):
    calls = []
    probes = []

    def probe():
        probes.append(len(probes))
        return len(probes) > 1

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (0, "Connected to BE#3 in Brussels.\n")
        if argv[1:] == ["connect", "--country", "AL", "--random"]:
            return (0, "Connected to AL#1 in Tirana.\n")
        return (1, "unexpected")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    ret = vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=probe)
    assert ret == 0
    assert ["connect", "--country", "BE", "--random"] in calls
    assert ["connect", "--country", "AL", "--random"] in calls
    assert state(tmp_path)["country"] == "AL"
    assert state(tmp_path)["rotations"] == [1_000_000]
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "target BE (BE#3) rate-limited on provider; trying next country" in log_text


def test_empty_countries_allowlist_uses_every_country(tmp_path):
    calls = []
    broad = "Iraq   IQ\nNetherlands   NL\n"
    status = "Status: Connected\nServer: NL#584 in Amsterdam\n"
    run = fake_run({"countries": (0, broad), "status": (0, status), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run) == 0
    assert ["connect", "--country", "IQ", "--random"] in calls


def test_rejected_candidates_do_not_use_the_hourly_budget(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "AT#12"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (0, "Connected to XX#1 in Elsewhere.\n")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0", "PI_VPN_ROTATE_MAX_PER_HOUR": "1"}
    ret = vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=lambda: False)
    assert ret == 1
    assert ["connect", "--country", "BE", "--random"] in calls
    assert ["connect", "--country", "AL", "--random"] in calls
    assert ["connect", "AT#12"] in calls
    assert "rotations" not in state(tmp_path)


def test_probe_opencode_classifies_responses(monkeypatch):
    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (0, "200\n"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "200"

    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (0, "429\n"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "429"

    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (0, "401\n"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "401"

    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (0, "500\n"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "500"

    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (1, "timeout"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "timeout"

    monkeypatch.setattr(vpn, "run_stdout", lambda argv, timeout: (0, "0\n"))
    assert vpn.probe_opencode(bun_bin="/fake/bun") == "timeout"

    monkeypatch.setattr(vpn.shutil, "which", lambda cmd: None)
    assert vpn.probe_opencode(bun_bin=None) == "missing_bun"


def test_probe_outage_aborts_rotation_immediately(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (0, "Connected to BE#3 in Brussels.\n")
        if argv[1:] == ["connect", "AT#12"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (1, "unexpected")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    ret = vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=lambda: "500")
    assert ret == 1
    assert ["connect", "--country", "BE", "--random"] in calls
    assert not any(c == ["connect", "--country", "AL", "--random"] for c in calls)
    assert ["connect", "AT#12"] in calls
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "probe failed with 500 (provider error); stopping rotation" in log_text


def test_probe_auth_error_aborts_rotation_immediately(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (0, "Connected to BE#3 in Brussels.\n")
        if argv[1:] == ["connect", "AT#12"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (1, "unexpected")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    ret = vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=lambda: "401")
    assert ret == 1
    assert ["connect", "--country", "BE", "--random"] in calls
    assert not any(c == ["connect", "--country", "AL", "--random"] for c in calls)
    assert ["connect", "AT#12"] in calls
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "probe failed with 401 (auth error); stopping rotation" in log_text


def test_probe_timeout_logs_timeout_and_retries_next_country(tmp_path):
    calls = []
    probes = []

    def probe():
        probes.append(len(probes))
        return "200" if len(probes) > 1 else "timeout"

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (0, "Connected to BE#3 in Brussels.\n")
        if argv[1:] == ["connect", "--country", "AL", "--random"]:
            return (0, "Connected to AL#1 in Tirana.\n")
        return (1, "unexpected")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    ret = vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=probe)
    assert ret == 0
    assert ["connect", "--country", "BE", "--random"] in calls
    assert ["connect", "--country", "AL", "--random"] in calls
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "target BE (BE#3) probe timed out; trying next country" in log_text


def test_missing_bun_disables_probe_and_rotates_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vpn.shutil, "which", lambda cmd: None)

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (0, "Connected to BE#3 in Brussels.\n")
        return (1, "unexpected")

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0", "PI_VPN_ROTATE_PROBE": "1"}
    payload = json.dumps({"provider": "opencode-free", "errorKind": "rate_limit"})
    ret = vpn.main([], environ, io.StringIO(payload), run, Clock().now, Clock().sleep)
    assert ret == 0
    assert calls == [["status"], ["countries", "list"], ["connect", "--country", "BE", "--random"]]
    assert state(tmp_path)["country"] == "BE"
    log_text = (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "bun not found; probe disabled" in log_text


FREE_PLAN_ERROR = "Location selection is not available on the free plan. Please use 'protonvpn connect'."


def test_free_plan_falls_back_to_plain_connect(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect", "--country", "BE", "--random"]:
            return (1, FREE_PLAN_ERROR)
        if argv[1:] == ["connect"]:
            return (0, "Connected to NL-FREE#7 in Amsterdam.\n")
        return (1, "unexpected")

    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert ["connect", "--country", "BE"] not in calls
    assert state(tmp_path)["country"] == "NL"
    assert state(tmp_path)["server"] == "NL-FREE#7"


def test_free_plan_restores_with_plain_connect(tmp_path):
    calls = []
    results = iter([(1, FREE_PLAN_ERROR), (1, "no route"), (0, "Connected to NL-FREE#7 in Amsterdam.\n")])

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        return next(results)

    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert calls[-1] == ["connect"]
    assert ["connect", "AT#12"] not in calls


def test_free_plan_landing_on_the_same_server_is_not_a_rotation(tmp_path):
    def run(argv, timeout):
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        if argv[1:] == ["connect"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (1, FREE_PLAN_ERROR)

    assert main(tmp_path, Clock(), run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert "at" not in state(tmp_path)
    assert "same server" in (tmp_path / "pi-vpn-rotate.log").read_text()


def session_payload(session):
    return json.dumps({"provider": "opencode", "session_id": session})


def test_every_session_adopts_a_rotation_once(tmp_path):
    seed(tmp_path, {"at": 995_000, "rotations": [995_000], "country": "AT"})
    calls = []
    for session in ("b", "c", "d"):
        assert main(tmp_path, Clock(1000.0), ok_run(calls), session_payload(session)) == 0
    assert calls == []
    assert state(tmp_path)["debouncedSessions"] == ["b", "c", "d"]


def test_a_session_failing_again_after_adopting_rotates(tmp_path):
    seed(tmp_path, {"at": 995_000, "rotations": [995_000], "country": "AT"})
    calls = []
    assert main(tmp_path, Clock(1000.0), ok_run(calls), session_payload("b"), PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert calls == []
    assert main(tmp_path, Clock(1002.0), ok_run(calls), session_payload("b"), PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert ["connect", "--country", "BE", "--random"] in calls


def fake_bun(tmp_path, stderr="warning: something\n"):
    script = tmp_path / "bun"
    script.write_text(f"#!/bin/sh\nprintf '%s' \"$2\" > {tmp_path}/script\necho 200\nprintf '{stderr}' >&2\n")
    script.chmod(0o755)
    return script


def test_probe_reads_the_status_from_stdout_only(tmp_path):
    assert vpn.probe_opencode(bun_bin=str(fake_bun(tmp_path))) == "200"


def test_probe_uses_the_model_that_failed_for_opencode_free(tmp_path):
    payload = json.dumps({"provider": "opencode-free", "model": "iceman-free"})
    assert main(tmp_path, Clock(), ok_run([]), payload, PI_VPN_ROTATE_PROBE="1", PI_VPN_ROTATE_BUN=str(fake_bun(tmp_path))) == 0
    assert 'model: "iceman-free"' in (tmp_path / "script").read_text()


def test_probe_keeps_the_default_model_for_paid_opencode(tmp_path):
    payload = json.dumps({"provider": "opencode", "model": "claude-opus"})
    assert main(tmp_path, Clock(), ok_run([]), payload, PI_VPN_ROTATE_PROBE="1", PI_VPN_ROTATE_BUN=str(fake_bun(tmp_path))) == 0
    assert 'model: "big-pickle"' in (tmp_path / "script").read_text()


def probe_run(calls):
    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["countries", "list"]:
            return (0, COUNTRIES)
        if argv[1:] == ["status"]:
            return (0, CONNECTED)
        return (0, "Connected to BE#3 in Brussels.\n")
    return run


def test_an_unexpected_probe_status_keeps_the_new_server(tmp_path):
    calls = []
    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    assert vpn.main([], environ, io.StringIO("{}"), probe_run(calls), Clock().now, Clock().sleep, probe=lambda: "404") == 0
    assert ["connect", "AT#12"] not in calls
    assert state(tmp_path)["country"] == "BE"


def test_the_loop_never_retries_a_rejected_country(tmp_path):
    calls = []
    countries = "Albania AL\nAustria AT\n"

    def run(argv, timeout):
        return (0, countries) if argv[1:] == ["countries", "list"] else probe_run(calls)(argv, timeout)

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    assert vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=lambda: "429") == 1
    assert calls.count(["connect", "--country", "AL", "--random"]) == 1


def test_candidates_stop_at_the_limit(tmp_path):
    calls = []
    countries = "Albania AL\nAustria AT\nBelgium BE\nCanada CA\nDenmark DK\n"

    def run(argv, timeout):
        return (0, countries) if argv[1:] == ["countries", "list"] else probe_run(calls)(argv, timeout)

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    assert vpn.main([], environ, io.StringIO("{}"), run, Clock().now, Clock().sleep, probe=lambda: "429") == 1
    assert len([c for c in calls if "--country" in c]) == vpn.MAX_CANDIDATES


def test_the_probe_waits_for_routes_to_settle(tmp_path):
    clock = Clock()
    seen = []

    def probe():
        seen.append(clock.t)
        return "200"

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    assert vpn.main([], environ, io.StringIO("{}"), probe_run([]), clock.now, clock.sleep, probe=probe) == 0
    assert seen == [1000.0 + vpn.SETTLE_S]


def test_a_spent_budget_starts_no_candidate_and_keeps_the_tunnel(tmp_path):
    calls = []
    clock = Clock()

    def run(argv, timeout):
        clock.t += vpn.ROTATE_BUDGET_S
        return probe_run(calls)(argv, timeout)

    environ = {"XDG_RUNTIME_DIR": str(tmp_path), "PI_VPN_ROTATE_COOLDOWN_S": "0"}
    assert vpn.main([], environ, io.StringIO("{}"), run, clock.now, clock.sleep, probe=lambda: "200") == 1
    assert not any(c[0] == "connect" for c in calls)
    assert "time budget spent" in (tmp_path / "pi-vpn-rotate.log").read_text()


def test_worst_case_rotation_fits_the_hook_timeout():
    assert vpn.ROTATE_BUDGET_S + 4 < 310
    assert vpn.LOCK_WAIT_S >= vpn.ROTATE_BUDGET_S


@pytest.fixture
def signal_setup(tmp_path, monkeypatch):
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    spawned = []
    monkeypatch.setattr(vpn.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(vpn.os, "_exit", fake_exit)
    lock = open(tmp_path / "lock", "a+")
    yield lock, spawned
    lock.close()
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def test_a_signal_mid_rotation_starts_a_detached_restore(tmp_path, signal_setup):
    lock, spawned = signal_setup
    pending = {"restore": ["protonvpn", "connect", "AT#12"]}
    vpn.restore_on_signal(pending, lock, vpn.Log(tmp_path / "log", Clock().now))
    with pytest.raises(SystemExit) as exit_info:
        signal.raise_signal(signal.SIGTERM)
    assert exit_info.value.code == 143
    argv, options = spawned[0]
    assert argv == ["protonvpn", "connect", "AT#12"]
    assert options["start_new_session"] is True
    assert options["pass_fds"] == (lock.fileno(),)


def test_a_signal_without_a_pending_restore_only_exits(tmp_path, signal_setup):
    lock, spawned = signal_setup
    vpn.restore_on_signal({}, lock, vpn.Log(tmp_path / "log", Clock().now))
    with pytest.raises(SystemExit):
        signal.raise_signal(signal.SIGTERM)
    assert spawned == []


def test_clean_flagged_drops_expired_entries():
    now = 5000.0
    flagged = {
        "IS#24": {"at": 1000.0, "ttl": 3600, "reason": "rate_limit"},
        "NL#5": {"at": 4000.0, "ttl": 3600, "reason": "rate_limit"},
    }
    assert vpn.clean_flagged(flagged, now) == {
        "NL#5": {"at": 4000.0, "ttl": 3600, "reason": "rate_limit"},
    }


def test_is_flagged_matches_server():
    now = 2000.0
    flagged = {
        "IS#24": {"at": 1000.0, "ttl": 3600, "reason": "rate_limit"},
        "DE#1": {"at": 1000.0, "ttl": 500, "reason": "rate_limit"},
    }
    assert vpn.is_flagged("IS#24", flagged, now) is True
    assert vpn.is_flagged("DE#1", flagged, now) is False
    assert vpn.is_flagged("NL#3", flagged, now) is False


def test_time_left_computes_remaining_seconds():
    now = 1200.0
    flagged = {
        "IS#24": {"at": 1000.0, "ttl": 3600, "reason": "rate_limit"},
    }
    assert vpn.time_left("IS#24", flagged, now) == 3400
    assert vpn.time_left("NL#3", flagged, now) == 0


def test_rotate_skips_known_flagged_server(tmp_path):
    calls = []
    # AT#12 is connected, next is BE which connects to BE#3, but BE#3 is already flagged
    replies = {
        "countries": (0, COUNTRIES),
        "status": (0, CONNECTED),
        "connect": (0, "Connected to BE#3 in Brussels.\n"),
    }
    clock = Clock(start=1000.0)
    log_file = tmp_path / "log"
    log = vpn.Log(log_file, clock.now)
    record = {"flagged": {"BE#3": {"at": 900.0, "ttl": 3600, "reason": "rate_limit"}}}
    res = vpn.rotate(
        tmp_path / "state.json", record, fake_run(replies, calls), clock.now, "protonvpn", log,
        ["BE"], has_iface=lambda path: True, get_ip=lambda: "1.2.3.4",
        probe=None, max_attempts=1, sleep=clock.sleep,
        deadline=clock.now() + 200, pending={},
    )
    assert res is False
    assert "target BE (BE#3) is known rate-limited" in log_file.read_text()


def test_free_plan_initial_connect_uses_connect(tmp_path):
    calls = []
    replies = {
        "status": (0, "Status: Disconnected\n"),
        "countries": (0, COUNTRIES),
    }

    def run(argv, timeout):
        calls.append(argv[1:])
        if argv[1:] == ["connect", "--country", "AL", "--random"]:
            return 1, "is not available on the free plan"
        if argv[1:] == ["connect"]:
            return 0, "Connected to NL-FREE#1 in Amsterdam.\nYour new IP address is 5.6.7.8.\n"
        return replies[argv[1]]

    assert main(tmp_path, Clock(), run, has_iface=lambda _: False) == 0
    # Must run plain connect, not disconnect
    assert ["connect"] in calls
    assert ["disconnect"] not in calls
    assert state(tmp_path)["server"] == "NL-FREE#1"


def test_decide_allows_retries_within_cooldown():
    record = {"at": 1000_000, "attemptAt": 1000_000}
    # Within 600s cooldown and past debounce (10s): retry=0 rejected, retry=1 allowed
    assert vpn.decide(record, 1020.0, "", 600, 5, retry=0) == (1, "cool-down")
    assert vpn.decide(record, 1020.0, "", 600, 5, retry=1) is None
    assert vpn.decide(record, 1020.0, "", 600, 5, retry=2) is None
    assert vpn.decide(record, 1020.0, "", 600, 5, retry=3, max_retries=3) == (1, "cool-down")
    # Immediate retry within 10s debounce: retry > 0 is not blocked by debounce
    assert vpn.decide(record, 1002.0, "", 600, 5, retry=1) is None


def test_max_candidates_fallback_on_invalid_env(tmp_path):
    calls = []
    # Should not crash on empty or non-numeric env var
    assert main(tmp_path, Clock(), ok_run(calls), PI_VPN_ROTATE_MAX_CANDIDATES="") == 0
    assert main(tmp_path, Clock(), ok_run(calls), PI_VPN_ROTATE_MAX_CANDIDATES="invalid") == 0


def test_rotate_avoids_restoring_rate_limited_server(tmp_path):
    calls = []
    run = fake_run({
        "countries": (0, COUNTRIES),
        "status": (0, CONNECTED),
        "connect": (0, CONNECT_OK),
    }, calls)
    clock = Clock(start=1000.0)
    log_file = tmp_path / "log"
    log = vpn.Log(log_file, clock.now)
    pending = {}
    record = {
        "flagged": {"AT#12": {"at": 900.0, "ttl": 3600, "reason": "rate_limit"}},
    }
    res = vpn.rotate(
        tmp_path / "state.json", record, run, clock.now, "protonvpn", log,
        ["BE"], has_iface=lambda path: True, get_ip=lambda: "1.2.3.4",
        probe=lambda: "429", max_attempts=1, sleep=clock.sleep,
        deadline=clock.now() + 200, pending=pending, flag_current=True,
    )
    assert res is False
    log_text = log_file.read_text()
    assert "original server AT#12 is rate-limited; keeping current connection" in log_text
    assert ["connect", "AT#12"] not in calls


def test_main_puts_the_signal_handlers_back(tmp_path):
    before = signal.getsignal(signal.SIGTERM)
    main(tmp_path, Clock(), ok_run([]))
    assert signal.getsignal(signal.SIGTERM) is before
