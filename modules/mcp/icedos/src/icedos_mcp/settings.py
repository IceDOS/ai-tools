"""Build host and storage settings from the JSON file the module writes."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

ENV = "ICEDOS_MCP_SETTINGS"

DEFAULTS: dict[str, dict[str, Any]] = {
    "build": {"hosts": [], "minFreeGb": 20, "pressureLimit": 20, "remoteExec": False},
    "storage": {"drives": {}, "priority": [], "reserveGb": 30, "largeGb": 5, "maxFlakeCopyMb": 200},
}


def load() -> dict[str, dict[str, Any]]:
    """Defaults overlaid with the file at ICEDOS_MCP_SETTINGS; a missing or broken file keeps the defaults."""
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    path = os.environ.get(ENV)
    if path:
        with contextlib.suppress(OSError, ValueError):
            for k, v in json.loads(Path(path).read_text()).items():
                if k in cfg and isinstance(v, dict):
                    cfg[k].update(v)
    return cfg
