"""Remote Nix build hosts: probe load and disk, pick one, build there, clean up after."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import sandbox as sandboxes
from .cli import run_streaming

CONFIG_ENV = "ICEDOS_MCP_BUILD_CONFIG"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")

# One fixed command for every host, so no tool input ever reaches a remote shell.
PROBE = (
    "nproc; cat /proc/loadavg; df -Pk /nix/store | tail -n 1; "
    "cat /proc/pressure/cpu /proc/pressure/io 2>/dev/null"
)

_AVG10_RE = re.compile(r"^some avg10=([\d.]+)", re.M)
_BUILDING_RE = re.compile(r"building '(/nix/store/[^']+\.drv)'")
_COPYING_RE = re.compile(r"copying path '(/nix/store/[^']+)'")
_LIVE_RE = re.compile(r"[Cc]annot delete path '(/nix/store/[^']+)'")


def settings() -> dict[str, Any]:
    """Hosts and thresholds from the JSON file the module points CONFIG_ENV at."""
    cfg: dict[str, Any] = {"hosts": [], "minFreeGb": 20, "pressureLimit": 20}
    path = os.environ.get(CONFIG_ENV)
    if path:
        with contextlib.suppress(OSError, ValueError):
            cfg.update(json.loads(Path(path).read_text()))
    return cfg


def parse_probe(text: str) -> dict[str, Any]:
    """Turn PROBE output into cores, load, free disk and the worst `some avg10` pressure."""
    lines = text.strip().splitlines()
    cores = int(lines[0])
    load1 = float(lines[1].split()[0])
    free_gb = int(lines[2].split()[3]) / 1024 / 1024
    pressure = max((float(v) for v in _AVG10_RE.findall(text)), default=0.0)
    return {"cores": cores, "load1": load1, "free_gb": round(free_gb, 1), "pressure": pressure}


async def _probe(argv: list[str], timeout: float = 15) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, TimeoutError, asyncio.TimeoutError) as e:
        return {"reachable": False, "error": str(e) or type(e).__name__}
    if proc.returncode != 0:
        return {"reachable": False, "error": err.decode(errors="replace").strip()[-300:]}
    try:
        return {"reachable": True, **parse_probe(out.decode(errors="replace"))}
    except (IndexError, ValueError) as e:
        return {"reachable": False, "error": f"cannot parse probe output: {e}"}


def _leases_dir() -> Path:
    d = sandboxes.sandboxes_dir() / ".builds"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active_builds() -> dict[str, int]:
    """Builds running per host across every MCP server of this user; stale leases are dropped."""
    counts: dict[str, int] = {}
    for f in _leases_dir().iterdir():
        try:
            host, pid, _ = f.name.rsplit(".", 2)
            alive = _pid_alive(int(pid))
        except ValueError:
            continue
        if not alive:
            f.unlink(missing_ok=True)
            continue
        counts[unquote(host)] = counts.get(unquote(host), 0) + 1
    return counts


@contextlib.contextmanager
def lease(host: str):
    """Mark a build on host as running while the block runs."""
    f = _leases_dir() / f"{quote(host, safe='')}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    f.touch()
    try:
        yield
    finally:
        f.unlink(missing_ok=True)


async def choose(need_gb: float = 0) -> dict[str, Any]:
    """Probe this PC and every host, then pick where a heavy build should run."""
    cfg = settings()
    limit = float(cfg["pressureLimit"])
    hosts: list[str] = list(cfg["hosts"])
    probes = await asyncio.gather(
        _probe(["sh", "-c", PROBE]),
        *(_probe(["ssh", *SSH_OPTS, h, PROBE]) for h in hosts),
    )
    busy = active_builds()

    local = {"host": "local", **probes[0], "active_builds": busy.get("local", 0)}
    if local.get("reachable"):
        reasons = []
        if local["load1"] > local["cores"]:
            reasons.append(f"load {local['load1']} above {local['cores']} cores")
        if local["pressure"] > limit:
            reasons.append(f"pressure avg10 {local['pressure']} above {limit:g}")
        local["busy"] = bool(reasons)
        local["reason"] = "; ".join(reasons) or "idle enough"

    min_free = float(cfg["minFreeGb"]) + need_gb
    rows: list[dict[str, Any]] = []
    for h, p in zip(hosts, probes[1:]):
        row = {"host": h, **p, "active_builds": busy.get(h, 0)}
        if not p.get("reachable"):
            row.update(ok=False, reason="unreachable")
        elif p["load1"] >= p["cores"] / 2:
            row.update(ok=False, reason=f"load {p['load1']} not below half of {p['cores']} cores")
        elif p["free_gb"] < min_free:
            row.update(ok=False, reason=f"{p['free_gb']} GB free, needs {min_free:g}")
        else:
            row.update(ok=True, reason="qualifies")
        rows.append(row)

    # Fewest running builds first spreads parallel agents, then the lowest load per core.
    ok = sorted((r for r in rows if r["ok"]), key=lambda r: (r["active_builds"], r["load1"] / r["cores"]))
    if not local.get("busy", False):
        pick, why = "local", "this PC is not busy"
    elif ok:
        pick, why = ok[0]["host"], f"this PC is busy ({local['reason']})"
    else:
        pick, why = "local", "this PC is busy but no host qualifies"
    return {"pick": pick, "why": why, "local": local, "hosts": rows}


# Paths each remote build added to its host, deleted by cleanup().
_created: dict[str, set[str]] = {}


def _outputs_of(drvs: list[str]) -> set[str]:
    """Output paths of local .drv files, for both `nix derivation show` JSON layouts."""
    if not drvs:
        return set()
    proc = subprocess.run(
        ["nix", "derivation", "show", *drvs], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        return set()
    data = json.loads(proc.stdout)
    data = data.get("derivations", data)
    paths: set[str] = set()
    for drv in data.values():
        for out in drv.get("outputs", {}).values():
            if p := out.get("path"):
                paths.add(p if p.startswith("/") else f"/nix/store/{p}")
    return paths


def record(host: str, log: str) -> set[str]:
    """Remember what a build put on host: built drvs and outputs, copied and substituted paths.

    Only paths the log shows arriving count, so paths the host already had are never deleted.
    """
    built = _BUILDING_RE.findall(log)
    paths = set(built) | set(_COPYING_RE.findall(log)) | _outputs_of(built)
    _created.setdefault(host, set()).update(paths)
    return paths


def build_argv(installable: str, host: str) -> list[str]:
    store = [] if host == "local" else ["--eval-store", "auto", "--store", f"ssh-ng://{host}"]
    return ["nix", "build", *store, "--no-link", "--json", installable]


async def build(installable: str, host: str, timeout: float, on_line=None) -> tuple[int, str, list[dict[str, Any]]]:
    """Run the build; the JSON result is the last stdout line that parses as a list."""
    with lease(host):
        rc, log, _ = await run_streaming(*build_argv(installable, host), timeout=timeout, on_line=on_line)
    result: list[dict[str, Any]] = []
    for line in reversed(log.splitlines()):
        if line.startswith("["):
            with contextlib.suppress(ValueError):
                result = json.loads(line)
                break
    return rc, log, result


def cleanup(host: str = "", timeout: float = 300) -> dict[str, str]:
    """Delete what this server's builds added to host (all hosts when empty). Live paths stay."""
    report: dict[str, str] = {}
    for h in [host] if host else list(_created):
        paths = _created.get(h, set())
        kept: list[str] = []
        msg = "nothing to delete"
        # A path another root still uses aborts the delete, so drop it and retry.
        for _ in range(20):
            if not paths:
                break
            proc = subprocess.run(
                ["nix", "store", "delete", "--store", f"ssh-ng://{h}", *sorted(paths)],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode == 0:
                msg = f"deleted {len(paths)} paths"
                paths = set()
                break
            live = set(_LIVE_RE.findall(proc.stderr)) & paths
            if not live:
                msg = proc.stderr.strip()[-500:]
                break
            kept += sorted(live)
            paths -= live
        _created[h] = paths
        if not paths:
            _created.pop(h, None)
        report[h] = msg + (f"; kept {len(kept)} live: {', '.join(kept[:5])}" if kept else "")
    return report
