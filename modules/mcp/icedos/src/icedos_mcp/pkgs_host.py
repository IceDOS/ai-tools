"""Evaluate and build packages on build hosts for pkgs_build and run_package."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import uuid
from pathlib import Path, PurePosixPath

from . import build_hosts as remote
from . import cli
from . import shell_run
from .cli import run_streaming

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")


def find_build_pkg_expr() -> str:
    """Path of icedos-build-pkg.nix wrapper, read from icedos dispatch scripts."""
    # shortcut: scrapes the baked script; upgrade when core exports the expr path
    icedos_bin = os.path.realpath(shutil.which("icedos") or "/run/current-system/sw/bin/icedos")
    with contextlib.suppress(OSError):
        with open(icedos_bin, "r", errors="replace") as f:
            if m_pkgs := re.search(r"(/nix/store/[^\s\x22\x27]+-pkgs)", f.read()):
                with open(m_pkgs.group(1), "r", errors="replace") as f_pkgs:
                    if m_build := re.search(r"(/nix/store/[^\s\x22\x27]+-build)", f_pkgs.read()):
                        with open(m_build.group(1), "r", errors="replace") as f_build:
                            if m_expr := re.search(r"(/nix/store/[^\s\x22\x27]+-icedos-build-pkg\.nix)", f_build.read()):
                                return m_expr.group(1)
    return ""


async def build_pkg_drv(path: str) -> tuple[str, str]:
    """(.drv path, error). Evaluates package.nix against system pkgs and icedosLib.packaging."""
    expr = find_build_pkg_expr()
    if not expr:
        return "", "cannot find icedos-build-pkg.nix from icedos pkgs build; rebuild the system or verify icedos is in PATH"
    proc = await asyncio.create_subprocess_exec(
        "nix-instantiate", "--argstr", "packagePath", str(Path(path).resolve()),
        "-E", f"(import {expr})",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    lines = out.decode(errors="replace").strip().splitlines()
    drv = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not drv.endswith(".drv"):
        return "", err.decode(errors="replace").strip()[-1500:] or "evaluation printed no .drv"
    return drv, ""


async def package_drv(attr: str, insecure: bool) -> tuple[str, str]:
    """(.drv path, error). Evaluates nixpkgs package attribute."""
    if not shell_run._ATTR_RE.match(attr):
        return "", f"invalid package attribute: {attr!r}"
    env = dict(os.environ, NIXPKGS_ALLOW_UNFREE="1")
    if insecure:
        env["NIXPKGS_ALLOW_INSECURE"] = "1"
    proc = await asyncio.create_subprocess_exec(
        "nix-instantiate", "<nixpkgs>", "-A", attr,
        env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    lines = out.decode(errors="replace").strip().splitlines()
    drv = lines[-1].strip() if lines else ""
    if proc.returncode != 0 or not drv.endswith(".drv"):
        return "", err.decode(errors="replace").strip()[-1500:] or "evaluation printed no .drv"
    return drv, ""


async def get_exe_name(attr: str) -> str:
    """Main binary name from nixpkgs meta.mainProgram, or empty."""
    if not shell_run._ATTR_RE.match(attr):
        return ""
    proc = await asyncio.create_subprocess_exec(
        "nix", "eval", "--raw", "--impure", "--expr",
        f"(let p = (import <nixpkgs> {{ config = {{ allowUnfree = true; }}; }}); in p.lib.getExe p.{attr})",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode == 0 and out.strip():
        return PurePosixPath(out.decode().strip()).name
    return ""


async def build_on_host(
    drv: str, host: str, timeout: float = 1800, store_path: str = ""
) -> tuple[int, str, str]:
    """(rc, clean_log, out_path). Builds drv on host and records paths for cleanup."""
    target = remote.remote_store(host, store_path)
    try:
        rc, log, result = await remote.build(
            remote.drv_installable(drv), host=host, timeout=timeout, store=store_path
        )
    except TimeoutError as e:
        partial = getattr(e, "partial_output", "")
        if target:
            remote.record(target, partial)
        raise TimeoutError(f"{e}\n--- last output ---\n{cli.summarize_log_tail(partial)}") from e
    if target:
        remote.record(target, log, result if store_path else None)
    clean = _ANSI_RE.sub("", log).strip()
    paths = remote.result_paths(result)[0]
    out_path = paths[0] if paths else ""
    return rc, clean, out_path


async def run_remote_binary(
    host: str, argv: list[str], timeout: float = 300, on_line=None
) -> tuple[int, str]:
    """Run an executable on host; a timeout kills the remote process group."""
    work = f"{shell_run.remote_root()}/run-{uuid.uuid4().hex[:8]}"
    shell_run._dirs.setdefault(host, set()).add(work)
    script = 'mkdir -p "$0" && echo $$ > "$0/pid"; exec "$@"'
    cmd = shlex.join(["setsid", "-w", "sh", "-c", script, work, *argv])
    done = False
    try:
        with remote.lease(host):
            rc, out, _ = await run_streaming(
                "ssh", *remote.SSH_OPTS, host, cmd, timeout=timeout, on_line=on_line
            )
        done = True
    finally:
        if not done:
            await shell_run.kill_remote(host, work)
        with contextlib.suppress(Exception):
            await shell_run._ssh(host, f"rm -rf {shlex.quote(work)}", timeout=15)
        shell_run._dirs.get(host, set()).discard(work)
    return rc, out


async def resolve_host_binary(
    host: str, out_path: str, binary_name: str = "", store_path: str = ""
) -> tuple[str, str]:
    """(binary path on host, error). Uses binary_name or looks up in bin/."""
    if binary_name:
        return f"{out_path}/bin/{binary_name}", ""
    bin_dir = f"{store_path}{out_path}/bin" if store_path else f"{out_path}/bin"
    rc, out = await shell_run._ssh(host, f"ls -1 {shlex.quote(bin_dir)}", timeout=15)
    if rc != 0:
        return "", f"cannot list binaries in {bin_dir}: {out.strip()}"
    bins = [b.strip() for b in out.strip().splitlines() if b.strip()]
    if not bins:
        return "", f"no executables in {bin_dir}"
    return f"{out_path}/bin/{bins[0]}", ""
