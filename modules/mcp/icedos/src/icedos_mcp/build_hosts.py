"""Nix builds off the root store: pick a remote host or a drive store, build there, clean up after."""

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
from . import settings as settings_file
from .cli import run_streaming

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


LOCAL = "local"


def settings() -> dict[str, Any]:
    """Hosts and thresholds. `hosts` holds only remote hosts; `order` is the whole list by priority.

    A "local" entry in the list places this PC; without one it comes first.
    """
    cfg = settings_file.load()["build"]
    order = list(cfg["hosts"])
    if LOCAL not in order:
        order.insert(0, LOCAL)
    return {**cfg, "hosts": [h for h in order if h != LOCAL], "order": order}


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

    # Fewer running builds first spreads parallel agents, then list order. This PC's own
    # builds already show in its load.
    rank = {h: i for i, h in enumerate(cfg["order"])}
    ok = [r for r in rows if r["ok"]]
    if not local.get("busy", False):
        ok.append({"host": LOCAL, "active_builds": 0})
    ok.sort(key=lambda r: (r["active_builds"], rank[r["host"]]))
    if not ok:
        pick, why = LOCAL, "this PC is busy but no host qualifies"
    elif ok[0]["host"] == LOCAL:
        pick, why = LOCAL, "this PC is not busy"
    elif local.get("busy", False):
        pick, why = ok[0]["host"], f"this PC is busy ({local['reason']})"
    else:
        pick, why = ok[0]["host"], "host comes before this PC in build.hosts"
    return {"pick": pick, "why": why, "order": cfg["order"], "local": local, "hosts": rows}


# Paths this server's builds added, keyed by store URL (ssh-ng://host or a drive store dir).
_created: dict[str, set[str]] = {}


def remote_store(host: str) -> str:
    return f"ssh-ng://{host}"


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


def record(store: str, log: str, result: list[dict[str, Any]] | None = None) -> set[str]:
    """Remember what a build put in store: built drvs and outputs, copied and substituted paths.

    On a host only paths the log shows arriving count, so paths it already had are never
    deleted. A drive store holds only agent builds, so the result's own paths count too.
    """
    built = _BUILDING_RE.findall(log)
    paths = set(built) | set(_COPYING_RE.findall(log)) | _outputs_of(built)
    for r in result or []:
        paths.add(r["drvPath"])
        paths.update(r.get("outputs", {}).values())
    _created.setdefault(store, set()).update(paths)
    return paths


def add(store: str, paths: set[str]) -> set[str]:
    _created.setdefault(store, set()).update(paths)
    return paths


def build_argv(installable: str, host: str, store: str = "") -> list[str]:
    """`store` is a drive store dir for a local build; a remote host brings its own store."""
    if host != "local":
        flags = ["--eval-store", "auto", "--store", remote_store(host)]
    else:
        flags = ["--store", store] if store else []
    return ["nix", "build", *flags, "--no-link", "--json", installable]


async def build(
    installable: str, host: str, timeout: float, on_line=None, store: str = ""
) -> tuple[int, str, list[dict[str, Any]]]:
    """Run the build; the JSON result is the last stdout line that parses as a list."""
    with lease(host):
        rc, log, _ = await run_streaming(*build_argv(installable, host, store), timeout=timeout, on_line=on_line)
    result: list[dict[str, Any]] = []
    for line in reversed(log.splitlines()):
        if line.startswith("["):
            with contextlib.suppress(ValueError):
                result = json.loads(line)
                break
    return rc, log, result


def gc(store: str, timeout: float = 1800) -> str:
    """Run `nix store gc` on a drive store, dropping every path nothing roots."""
    proc = subprocess.run(["nix", "store", "gc", "--store", store], capture_output=True, text=True, timeout=timeout)
    return gc_summary(proc)


def _full(p: str) -> str:
    return p if p.startswith("/") else f"/nix/store/{p}"


def _references(store: str, paths: set[str], timeout: float) -> dict[str, set[str]] | None:
    """References of each path still valid in store; None when the store cannot be queried."""
    proc = subprocess.run(
        ["nix", "path-info", "--json", "--store", store, *sorted(paths)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    data = data.get("info", data) if isinstance(data, dict) else {}
    return {_full(p): {_full(r) for r in info.get("references", [])} for p, info in data.items() if info}


def _delete(store: str, paths: set[str], timeout: float) -> tuple[set[str], set[str], str]:
    """One `nix store delete`, dropping paths it reports alive until the rest goes.

    Returns (deleted, alive, error). Nix refuses the whole call over one live path.
    """
    pending, alive = set(paths), set()
    while pending:
        proc = subprocess.run(
            ["nix", "store", "delete", "--store", store, *sorted(pending)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            return pending, alive, ""
        live = set(_LIVE_RE.findall(proc.stderr)) & pending
        if not live:
            return set(), alive | pending, proc.stderr.strip()[-500:]
        alive |= live
        pending -= live
    return set(), alive, ""


def cleanup(store: str = "", timeout: float = 300) -> dict[str, str]:
    """Delete what this server's builds added to store (every store when empty). Live paths stay.

    Deletes in rounds, paths nothing else in the set references first, since a path whose
    referrer still exists counts as alive.
    """
    report: dict[str, str] = {}
    for h in [store] if store else list(_created):
        if not _created.get(h):
            report[h] = "nothing to delete"
            continue
        refs = _references(h, _created[h], timeout)
        if refs is None:
            report[h] = "cannot query the store; paths kept for a later cleanup"
            continue
        remaining, kept, deleted, error = set(refs), set(), 0, ""
        while remaining and not error:
            leaves = {p for p in remaining if not any(p in refs[q] for q in remaining if q != p)}
            if not leaves:
                break
            gone, alive, error = _delete(h, leaves, timeout)
            deleted += len(gone)
            kept |= alive
            remaining -= leaves
        kept |= remaining
        if kept:
            _created[h] = kept
        else:
            _created.pop(h, None)
        msg = f"deleted {deleted} paths"
        if kept:
            msg += f"; kept {len(kept)} live: {', '.join(sorted(kept)[:5])}"
        report[h] = msg + (f"; error: {error}" if error else "")
    return report


def gc_summary(proc: subprocess.CompletedProcess) -> str:
    """The "N store paths deleted, X freed" line; Nix may print notes after it."""
    out = (proc.stdout + proc.stderr).strip().splitlines()
    if proc.returncode != 0:
        return f"gc failed: {' '.join(out[-3:])}"
    return next((line for line in reversed(out) if "freed" in line), out[-1] if out else "done")
