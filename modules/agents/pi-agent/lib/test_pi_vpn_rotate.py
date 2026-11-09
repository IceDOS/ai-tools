import fcntl
import io
import json

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
    environ = {"XDG_RUNTIME_DIR": str(tmp_path), **env}
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
    assert ["connect", "--country", "BE"] in calls
    assert state(tmp_path) == {
        "at": 1_000_000, "attemptAt": 1_000_000, "rotations": [1_000_000], "country": "BE",
        "server": "BE#3", "location": "Brussels", "egressIp": "1.2.3.4",
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
    assert ["connect", "--country", "BE"] in calls


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


def test_disconnected_vpn_skips_connect_and_exits_one(tmp_path):
    calls = []
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, "Status: Disconnected\n"), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run, has_iface=lambda _: False) == 1
    assert calls == [["status"]]
    assert "VPN is disconnected; skipping rotation" in (tmp_path / "pi-vpn-rotate.log").read_text()


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
    assert ["connect", "--country", "BE"] in calls
    assert "rotation failed" in (tmp_path / "pi-vpn-rotate.log").read_text()
    assert "at" not in state(tmp_path)
    assert state(tmp_path)["attemptAt"] == 1_000_000
    assert state(tmp_path)["rotations"] == [1_000_000]


def test_interrupted_connect_records_attempt_before_running(tmp_path):
    def run_with_abort(cmd, timeout=0):
        if cmd[1] == "status":
            return 0, CONNECTED
        if cmd[1] == "countries":
            return 0, COUNTRIES
        if cmd[1] == "connect":
            s = state(tmp_path)
            assert s["attemptAt"] == 1_000_000
            assert s["rotations"] == [1_000_000]
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


def test_failed_attempts_count_toward_hourly_cap(tmp_path):
    calls = []
    clock = Clock()
    run = fake_run({"countries": (0, COUNTRIES), "status": (0, CONNECTED), "connect": (1, "no route")}, calls)
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert main(tmp_path, clock, run, PI_VPN_ROTATE_COOLDOWN_S="0") == 1
    assert "hourly cap" in (tmp_path / "pi-vpn-rotate.log").read_text()


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
    assert ["connect", "--country", "AL"] in calls


def test_server_with_region_suffix_extracts_base_country(tmp_path):
    calls = []
    status = "Status: Connected\nServer: US-NY#12 in New York\n"
    countries = "United States   US\nNetherlands     NL\n"
    run = fake_run({"countries": (0, countries), "status": (0, status), "connect": (0, CONNECT_OK)}, calls)
    assert main(tmp_path, Clock(), run) == 0
    assert ["connect", "--country", "NL"] in calls


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
        if argv[1:] == ["connect", "--country", "BE"]:
            return (1, "timeout")
        if argv[1:] == ["connect", "AT#12"]:
            return (0, "Connected to AT#12 in Vienna.\n")
        return (1, "unexpected")
    assert main(tmp_path, Clock(), run) == 1
    assert ["connect", "--country", "BE"] in calls
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
        if argv[1:] == ["connect", "--country", "BE"]:
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
    assert calls[-2] == ["connect", "--country", "BE"]
    assert calls[-1] == ["connect", "AT#12"]

    # On next rate limit, BE was recorded as failedTarget, so it should try AL
    run2 = fake_run({
        "countries": (0, COUNTRIES),
        "status": (0, CONNECTED),
        "connect": (0, CONNECT_OK),
    }, calls)
    assert main(tmp_path, clock, run2, PI_VPN_ROTATE_COOLDOWN_S="0") == 0
    assert ["connect", "--country", "AL"] in calls


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
