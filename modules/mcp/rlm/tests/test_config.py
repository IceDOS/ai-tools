from pathlib import Path

from rlm_mcp.config import Settings


def test_from_env_reads_all_fields(tmp_path):
    s = Settings.from_env({
        "RLM_AGY": "/x/agy",
        "RLM_KERNEL": "/x/k.py",
        "RLM_PYTHON": "/x/python3",
        "PLUGIN_DATA": str(tmp_path),
        "RLM_MAX_DEPTH": "3",
        "PI_RLM_DEPTH": "1",
        "RLM_CHILD_TIMEOUT": "5m",
        "RLM_CHILD_LOG": "0",
        "RLM_SNAPSHOTS": "0",
        "RLM_SELF": "/x/rlm-mcp",
    })
    assert s.agy == "/x/agy"
    assert s.kernel == "/x/k.py"
    assert s.python == "/x/python3"
    assert s.data_dir == tmp_path
    assert s.max_depth == 3
    assert s.depth == 1
    assert s.child_timeout == "5m"
    assert s.child_log is False
    assert s.snapshots is False
    assert s.self_cmd == ["/x/rlm-mcp"]


def test_defaults_without_plugin_data(tmp_path):
    s = Settings.from_env({"RLM_KERNEL": "/x/k.py", "HOME": str(tmp_path)})
    assert s.data_dir == Path(tmp_path) / ".local/share/icedos-rlm"
    assert s.max_depth == 2
    assert s.depth == 0
    assert s.child_log is True
    assert s.snapshots is True
    assert s.agy == "agy"
