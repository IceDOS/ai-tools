import json
import os
import time

import prime_agent_status as pas

SNAP = json.dumps({
    "ts": 1000, "interval": 2, "currency": "€",
    "ai": {"active": True, "watts": 27, "windows": [{"label": "1h", "wh": 10, "cost": 0.0016}]},
})


def test_fresh_snapshot_gives_watts_currency_and_window_costs():
    assert pas.ai_reading(SNAP, 1003) == {"currency": "€", "watts": 27.0, "windows": [("1h", 0.0016)]}


def test_snapshot_older_than_three_intervals_is_ignored():
    assert pas.ai_reading(SNAP, 1007) is None


def test_bad_json_or_missing_ai_is_ignored():
    assert pas.ai_reading("{", 1003) is None
    assert pas.ai_reading(json.dumps({"ts": 1000, "interval": 2}), 1003) is None


def test_lease_touch_and_drop(tmp_path):
    pas.touch_lease(42, tmp_path)
    assert (tmp_path / "prime-agent-42").exists()
    pas.drop_lease(42, tmp_path)
    assert not (tmp_path / "prime-agent-42").exists()


def test_bar_fills_and_colors_by_severity():
    assert pas.bar(85).count("▰") == 9
    assert "\x1b[31m" in pas.bar(85)
    assert "\x1b[32m" in pas.bar(10)


def line(role, provider, model, ts, **message):
    return json.dumps({"type": "message", "timestamp": ts,
                       "message": {"role": role, "provider": provider, "model": model, **message}},
                      separators=(",", ":"))


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def test_pool_count_includes_only_recent_assistant_messages_of_the_pool(tmp_path):
    now = 1_800_000_000
    (tmp_path / "a.jsonl").write_text("\n".join([
        line("assistant", "opencode", "big-pickle", iso(now - 60)),
        line("assistant", "opencode", "mimo-v2.5-free", iso(now - 120)),
        line("assistant", "opencode", "big-pickle", iso(now - 6 * 3600)),
        line("assistant", "opencode", "other-free", iso(now - 60)),
        line("user", "opencode", "big-pickle", iso(now - 60)),
    ]) + "\n")
    os.utime(tmp_path / "a.jsonl", (now, now))
    assert pas.count_pool_requests(tmp_path, ["big-pickle", "mimo-v2.5-free"], now - 5 * 3600) == 2


def failure(ts_ms, status, retry_ms):
    return line("assistant", "opencode", "big-pickle", iso(ts_ms / 1000), stopReason="error",
                diagnostics=[{"type": "provider_stream_failure", "timestamp": ts_ms,
                              "details": {"kind": "rate_limit", "status": status, "retryAfterMs": retry_ms}}])


def test_rate_limit_reset_comes_from_the_newest_429(tmp_path):
    now = 1_800_000_000
    f = tmp_path / "s.jsonl"
    f.write_text(failure((now - 10) * 1000, 429, 60_000) + "\n")
    assert pas.rate_limit_reset(f, now) == now + 50
    f.write_text(failure((now - 100) * 1000, 429, 60_000) + "\n")
    assert pas.rate_limit_reset(f, now) is None
    f.write_text(failure((now - 10) * 1000, 429, 60_000) + "\n"
                 + line("assistant", "opencode", "big-pickle", iso(now - 5), stopReason="stop") + "\n")
    assert pas.rate_limit_reset(f, now) is None


def test_energy_integrates_short_gaps_only(tmp_path):
    path = tmp_path / "energy.json"
    pas.record_energy(path, 100.0, 1_800_000_000)
    pas.record_energy(path, 100.0, 1_800_000_002)
    state = pas.record_energy(path, 100.0, 1_800_000_060)
    assert pas.window_joules(state, 1_800_000_060, 1, "h") == 200.0


def test_month_window_clamps_to_month_end():
    import datetime
    start = pas.window_start(datetime.datetime(2026, 3, 31, 12).timestamp(), 1, "M")
    assert datetime.datetime.fromtimestamp(start) == datetime.datetime(2026, 2, 28, 12)


def test_session_tokens_are_read_incrementally(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(line("assistant", "x", "y", iso(1), usage={"input": 10, "output": 5}) + "\n")
    assert pas.session_tokens(f, tmp_path / "state") == (10, 5)
    with f.open("a") as fh:
        fh.write(line("assistant", "x", "y", iso(2), usage={"input": 1, "output": 1}) + "\n")
    assert pas.session_tokens(f, tmp_path / "state") == (11, 6)


def test_sessions_dir_follows_the_account_dir():
    cfg = {"sessionsDir": "/real/sessions"}
    assert pas.sessions_dir(cfg, {}) == "/real/sessions"
    assert pas.sessions_dir(cfg, {"PRIME_AGENT_CODING_AGENT_DIR": "/acct"}) == "/acct/sessions"
    env = {"PRIME_AGENT_CODING_AGENT_DIR": "/acct", "PRIME_AGENT_SESSION_DIR": "/s"}
    assert pas.sessions_dir(cfg, env) == "/s"


def test_render_paid_model_shows_cost_line_only(tmp_path):
    cfg = {"showCost": True, "alimit": False, "alimitProviders": {}, "sessionsDir": str(tmp_path),
           "freePools": {}, "freeCaps": {}, "freeCapDefault": 1000, "power": None}
    payload = {"model": {"provider": "anthropic", "id": "claude"}, "costUsd": 0.5,
               "turnActive": False, "pid": 1, "cwd": "/", "sessionFile": None}
    lines = pas.render(cfg, payload, 1_800_000_000, tmp_path / "state")
    assert len(lines) == 1
    assert "$0.50" in lines[0]


def rows_cfg(tmp_path, commands):
    return {"showCost": True, "alimit": False, "alimitProviders": {}, "sessionsDir": str(tmp_path),
            "freePools": {}, "freeCaps": {}, "freeCapDefault": 1000, "power": None, "extraRows": commands}


ROWS_PAYLOAD = {"model": {"provider": "anthropic", "id": "claude"}, "costUsd": 0.5,
                "turnActive": False, "pid": 1, "cwd": "/", "sessionFile": None}


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def test_extra_rows_append_each_stdout_line(tmp_path):
    command = script(tmp_path, "rows", "echo 'vpn BE#3'; echo second")
    lines = pas.render(rows_cfg(tmp_path, [command]), ROWS_PAYLOAD, 1_800_000_000, tmp_path / "state")
    assert lines[1:] == ["vpn BE#3", "second"]


def test_extra_rows_skip_failing_silent_or_missing_commands(tmp_path):
    commands = [script(tmp_path, "fails", "echo nope; exit 1"), script(tmp_path, "silent", "true"),
                str(tmp_path / "missing")]
    lines = pas.render(rows_cfg(tmp_path, commands), ROWS_PAYLOAD, 1_800_000_000, tmp_path / "state")
    assert len(lines) == 1

