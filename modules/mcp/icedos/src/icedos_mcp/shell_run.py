"""Run a command in a Nix shell here or on a build host, and delete the work dirs it leaves."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from . import build_hosts as remote
from . import storage
from .cli import run_streaming

# nixpkgs attribute paths, e.g. "cargo" or "python3Packages.pytest".
_ATTR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'+-]*(\.[A-Za-z_][A-Za-z0-9_'+-]*)*$")
_STORE_ROOT_RE = re.compile(r"^(/nix/store/[^/]+)")

PKGS_EXPR = """{ names, insecure }:
let
  pkgs = import <nixpkgs> {
    config = { allowUnfree = true; } // (if insecure == "1" then { allowInsecurePredicate = _: true; } else { });
  };
  get = n: pkgs.lib.getAttrFromPath (pkgs.lib.splitString "." n) pkgs;
in
pkgs.mkShell { packages = map get (builtins.filter (n: n != "") (pkgs.lib.splitString " " names)); }
"""

# Synced with the source; .gitignore'd files and dirs stay on each side, so `target/` persists.
SYNC_FILTERS = ("--filter=:- .gitignore", "--exclude=/.git", "--exclude=result", "--exclude=result-*")

# Work dirs this server made: per host, and local scratch dirs under "local".
_dirs: dict[str, set[str]] = {}


def remote_root() -> str:
    return f"/tmp/icedos-mcp-{getpass.getuser()}"


def key(cwd: str) -> str:
    """Stable per source dir, so later runs reuse the remote copy and its build outputs."""
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def rsync_bin() -> str:
    return os.environ.get("ICEDOS_MCP_RSYNC") or shutil.which("rsync") or "rsync"


def system() -> str:
    return f"{platform.machine()}-linux"


def check_args(packages: list[str], flake: str, fetch: list[str]) -> str:
    """Why the arguments are refused, or ""."""
    if bool(packages) == bool(flake):
        return "pass exactly one of packages or flake"
    if bad := [p for p in packages if not _ATTR_RE.match(p)]:
        return f"not a nixpkgs attribute path: {', '.join(bad)}"
    if flake.startswith("-"):
        return "flake must be a flake ref, not a flag"
    for f in fetch:
        p = PurePosixPath(f)
        if not f or p.is_absolute() or ".." in p.parts:
            return f"fetch paths must be relative and stay inside cwd: {f!r}"
    return ""


def flake_ref(flake: str) -> str:
    """A ref without a fragment means its default devShell, like `nix develop`."""
    return flake if "#" in flake else f"{flake}#devShells.{system()}.default"


async def env_drv(packages: list[str], flake: str, insecure: bool) -> tuple[str, str]:
    """(.drv of the shell environment, error). Evaluates here, against this PC's pinned nixpkgs."""
    if packages:
        argv = ["nix-instantiate", "--expr", PKGS_EXPR, "--argstr", "names", " ".join(packages),
                "--argstr", "insecure", "1" if insecure else "0"]
    else:
        argv = ["nix", "path-info", "--derivation", flake_ref(flake)]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    lines = out.decode(errors="replace").split()
    drv = lines[-1] if lines else ""
    if proc.returncode != 0 or not drv.endswith(".drv"):
        return "", err.decode(errors="replace").strip()[-1500:] or "evaluation printed no .drv"
    return drv, ""


def local_scratch(cwd: str, need_gb: float) -> str:
    """A scratch dir on a secondary SSD for a local run, or "" when no room is asked for."""
    if need_gb <= 0:
        return ""
    d = storage.pick(need_gb, "random", f"shell-run-{key(cwd)}")["dir"]
    (Path(d) / "tmp").mkdir(exist_ok=True)
    _dirs.setdefault(remote.LOCAL, set()).add(d)
    return d


async def run_local(drv: str, command: str, cwd: str, scratch: str, timeout: float, on_line) -> tuple[int, str]:
    env = dict(os.environ)
    if scratch:
        env.update(ICEDOS_SCRATCH=scratch, TMPDIR=f"{scratch}/tmp")
    with remote.lease(remote.LOCAL):
        rc, log, _ = await run_streaming("nix-shell", drv, "--run", command, cwd=cwd, env=env,
                                         timeout=timeout, on_line=on_line)
    return rc, log


async def _ssh(host: str, script: str, timeout: float = 60) -> tuple[int, str]:
    rc, out, _ = await run_streaming("ssh", *remote.SSH_OPTS, host, script, timeout=timeout)
    return rc, out


async def prepare(host: str, cwd: str, need_gb: float) -> tuple[str, str]:
    """(work dir on host, error). Refuses when the host's /tmp lacks need_gb."""
    work = f"{remote_root()}/{key(cwd)}"
    q = shlex.quote(work)
    rc, out = await _ssh(host, f"mkdir -p {q}/src {q}/tmp {q}/scratch && df -Pk {q} | tail -n 1")
    if rc != 0:
        return "", f"cannot create {work} on {host}: {out.strip()[-300:]}"
    _dirs.setdefault(host, set()).add(work)
    try:
        free_gb = int(out.split()[-3]) / 1024 / 1024
    except (IndexError, ValueError):
        return "", f"cannot read free space of {work} on {host}: {out.strip()[-300:]}"
    if free_gb < need_gb:
        return "", f"{host} has {free_gb:.1f} GB free under {remote_root()}, the run needs {need_gb:g}"
    return work, ""


async def copy_closure(host: str, paths: list[str], timeout: float, on_line=None) -> tuple[int, str]:
    """`nix copy` to the host and record the paths it sent; paths the host had print nothing."""
    rc, log, _ = await run_streaming("nix", "copy", "--substitute-on-destination", "--to",
                                     remote.remote_store(host), *paths, timeout=timeout, on_line=on_line)
    remote.record(remote.remote_store(host), log)
    return rc, log


async def remote_rsync(host: str) -> str:
    """Path of this PC's rsync on the host, copied there; plain `rsync` when the copy fails."""
    local = os.path.realpath(rsync_bin())
    m = _STORE_ROOT_RE.match(local)
    if m and (await copy_closure(host, [m.group(1)], 300))[0] == 0:
        return local
    return "rsync"


async def sync(host: str, cwd: str, work: str, rsync_path: str, timeout: float) -> tuple[int, str]:
    rc, out, _ = await run_streaming(
        rsync_bin(), "-a", "--delete", *SYNC_FILTERS, "-e", shlex.join(["ssh", *remote.SSH_OPTS]),
        f"--rsync-path={rsync_path}", f"{cwd.rstrip('/')}/", f"{host}:{work}/src/", timeout=timeout,
    )
    return rc, out


def remote_command(work: str, drv: str, command: str) -> str:
    """One quoted string for the host's login shell; setsid makes the run its own process group."""
    script = (
        'echo $$ > "$0/pid"; cd "$0/src" && '
        'exec env ICEDOS_SCRATCH="$0/scratch" TMPDIR="$0/tmp" nix-shell "$1" --run "$2"'
    )
    return shlex.join(["setsid", "-w", "sh", "-c", script, work, drv, command])


async def kill_remote(host: str, work: str) -> None:
    pid = f"{shlex.quote(work)}/pid"
    with contextlib.suppress(Exception):
        await _ssh(host, f"p=$(cat {pid}) && kill -TERM -- -$p; sleep 3; kill -KILL -- -$p 2>/dev/null; true", 30)


async def run_remote(host: str, work: str, drv: str, command: str, timeout: float, on_line) -> tuple[int, str]:
    """Run on host; a timeout or a cancelled call kills the remote process group."""
    done = False
    try:
        with remote.lease(host):
            rc, log, _ = await run_streaming("ssh", *remote.SSH_OPTS, host, remote_command(work, drv, command),
                                             timeout=timeout, on_line=on_line)
        done = True
    finally:
        if not done:
            await kill_remote(host, work)
    remote.record(remote.remote_store(host), log)
    return rc, log


async def fetch_back(host: str, work: str, cwd: str, paths: list[str], timeout: float) -> tuple[int, str]:
    """Copy paths from the host's copy of cwd into the same place under cwd."""
    srcs = [f"{host}:{work}/src/./{p}" for p in paths]
    rc, out, _ = await run_streaming(
        rsync_bin(), "-aR", "-e", shlex.join(["ssh", *remote.SSH_OPTS]), *srcs, f"{cwd.rstrip('/')}/",
        timeout=timeout,
    )
    return rc, out


def cleanup(target: str = "", timeout: float = 120) -> dict[str, str]:
    """Delete the work dirs runs made on target (every host and this PC when empty)."""
    report: dict[str, str] = {}
    for where in [target] if target else list(_dirs):
        dirs = sorted(_dirs.get(where, ()))
        if not dirs:
            continue
        if where == remote.LOCAL:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
            report["local work dirs"] = f"deleted {len(dirs)}"
            _dirs.pop(where, None)
            continue
        quoted = " ".join(shlex.quote(d) for d in dirs if d.startswith(remote_root() + "/"))
        script = f"rm -rf -- {quoted}; rmdir {shlex.quote(remote_root())} 2>/dev/null; true"
        try:
            proc = subprocess.run(["ssh", *remote.SSH_OPTS, where, script],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            report[f"{where} work dirs"] = "ssh timed out; dirs kept for a later cleanup"
            continue
        if proc.returncode == 0:
            report[f"{where} work dirs"] = f"deleted {len(dirs)}"
            _dirs.pop(where, None)
        else:
            report[f"{where} work dirs"] = f"kept: {proc.stderr.strip()[-300:]}"
    return report
