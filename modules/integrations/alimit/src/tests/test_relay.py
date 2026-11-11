import json

from alimit import cli, relay


def _rec(root, rid, pid, group=None):
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({"id": rid, "kind": "claude", "pid": pid, "group": group}))


def test_no_record(tmp_path):
    assert relay.segment([10], root=tmp_path, alive=lambda p: True) is None


def test_single(tmp_path):
    _rec(tmp_path, "3f2a9", 20)
    assert relay.segment([10, 20], root=tmp_path, alive=lambda p: True) == "relay 3f2a9"


def test_peers_count_live_only(tmp_path):
    _rec(tmp_path, "3f2a9", 20, "g1")
    _rec(tmp_path, "aaaa1", 30, "g1")
    _rec(tmp_path, "bbbb2", 40, "g1")
    alive = lambda p: p != 40
    assert relay.segment([20], root=tmp_path, alive=alive) == "relay 3f2a9 · 1 peer"
    assert relay.segment([20], root=tmp_path, alive=lambda p: True) == "relay 3f2a9 · 2 peers"


def test_disabled(tmp_path, monkeypatch):
    _rec(tmp_path, "3f2a9", 20)
    monkeypatch.setenv("ALIMIT_RELAY_SEGMENT", "0")
    assert relay.segment([20], root=tmp_path, alive=lambda p: True) is None


def test_render_prompt_relay(monkeypatch):
    monkeypatch.setattr(cli.relay, "segment", lambda: "relay 3f2a9 · 1 peer")
    out = cli.render_prompt({}, [], 0)
    assert "relay 3f2a9 · 1 peer" in out
