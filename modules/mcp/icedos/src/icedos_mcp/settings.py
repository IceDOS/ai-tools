"""Build host and storage settings from the JSON file the module writes."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

ENV = "ICEDOS_MCP_SETTINGS"
# Same path on every IceDOS host, written by the module.
HOST_FILE = "/etc/icedos/mcp-settings.json"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")

DEFAULTS: dict[str, dict[str, Any]] = {
    "build": {"hosts": [], "minFreeGb": 20, "pressureLimit": 20, "remoteExec": False},
    "storage": {"drives": {}, "priority": [], "reserveGb": 30, "largeGb": 5, "maxFlakeCopyMb": 200},
}


def load() -> dict[str, dict[str, Any]]:
    """Defaults overlaid with the file at ICEDOS_MCP_SETTINGS; a missing or broken file keeps the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    path = os.environ.get(ENV)
    if path:
        with contextlib.suppress(OSError, ValueError):
            for k, v in json.loads(Path(path).read_text()).items():
                if k in cfg and isinstance(v, dict):
                    cfg[k].update(v)
    if "drives" in cfg["storage"] and isinstance(cfg["storage"]["drives"], dict):
        cfg["storage"]["drives"] = {m.rstrip("/") or "/": io for m, io in cfg["storage"]["drives"].items()}
    if "priority" in cfg["storage"] and isinstance(cfg["storage"]["priority"], list):
        cfg["storage"]["priority"] = [m.rstrip("/") or "/" for m in cfg["storage"]["priority"]]
    return cfg


_hosts: dict[str, dict[str, Any]] = {}


def storage(host: str = "local") -> dict[str, Any]:
    """storage block of this PC, or of a host read over ssh once per server run.

    A host without the file keeps drives={} and priority=[], so it uses its own store.
    """
    if host == "local":
        return load()["storage"]
    if host not in _hosts:
        cfg = copy.deepcopy(DEFAULTS["storage"])
        should_cache = False
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError, AttributeError):
            proc = subprocess.run(
                ["ssh", *SSH_OPTS, host, f"cat {HOST_FILE}"], capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0:
                block = json.loads(proc.stdout).get("storage")
                if isinstance(block, dict):
                    cfg.update(block)
                if "drives" in cfg and isinstance(cfg["drives"], dict):
                    cfg["drives"] = {m.rstrip("/") or "/": io for m, io in cfg["drives"].items()}
                if "priority" in cfg and isinstance(cfg["priority"], list):
                    cfg["priority"] = [m.rstrip("/") or "/" for m in cfg["priority"]]
                should_cache = True
            elif proc.returncode == 1 and "No such file" in proc.stderr:
                should_cache = True
        if should_cache:
            _hosts[host] = cfg
        return cfg
    return _hosts[host]
