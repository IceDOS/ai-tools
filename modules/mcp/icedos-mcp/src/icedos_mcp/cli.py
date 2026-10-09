"""Subprocess wrappers for the icedos CLI and related Nix commands."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from pathlib import Path

from .config import find_config_root


def _kill_proc_group(
    proc: asyncio.subprocess.Process, *, only_if_running: bool = False
) -> None:
    """Best-effort kill of the whole process group on timeout / error.

    ``start_new_session=True`` at spawn makes pgid == pid, so signalling the
    group reaches the wrapper shell and any surviving descendants. From an
    unprivileged server this cannot reach sudo/root-elevated descendants
    (e.g. ``icedos gc``'s ``nh clean all``), so callers must not treat a
    timeout as proof that everything stopped.

    Pass ``only_if_running=True`` for the opportunistic cleanup on the
    success path: asyncio reaps the leader as soon as it exits (setting
    ``returncode``) even while group members still hold the pipes, so the
    timeout branches must call it unconditionally.

    Unconditional callers accept a small pid-reuse window: if asyncio already
    reaped the leader and the OS recycled its pid into a new session leader
    owned by the same user, ``killpg`` could signal an unrelated group.
    """
    if only_if_running and proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass



def _find_icedos_bin() -> str:
    """Find the icedos binary."""
    path = shutil.which("icedos")
    if path:
        return path
    raise FileNotFoundError(
        "icedos command not found. Ensure IceDOS is installed and on PATH."
    )


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_NOM_FRAME_RE = re.compile(r"^⏱|^┏|^┃|^┗|^┣")


def summarize_log_tail(partial: str, max_input: int = 200_000, max_lines: int = 40) -> str:
    """Condense a partial build log for a timeout error message.

    Strips ANSI escapes, drops nom's per-frame redraw spam (timer ticks / graph
    frames), and bounds the result to the last ``max_lines`` non-empty lines, so
    even an hour-long build times out to a readable, size-capped tail. Mirrors
    ``summarize_build`` but keeps the length guarantee regardless of how many
    lines match a summary keyword.
    """
    clean = _ANSI_RE.sub("", partial[-max_input:])
    lines = [
        ln.rstrip()
        for ln in clean.splitlines()
        if ln.strip() and not _NOM_FRAME_RE.match(ln)
    ]
    return "\n".join(lines[-max_lines:])


def summarize_build(stdout: str, stderr: str, tail: int = 40) -> str:
    """Condense a nom build log: strip ANSI, keep summary lines + a short tail."""
    clean = _ANSI_RE.sub("", stdout + "\n" + stderr)
    lines = [ln.rstrip() for ln in clean.splitlines()]
    # drop the per-frame progress spam (timer ticks, redraw graph frames)
    keep = [ln for ln in lines if ln.strip() and not _NOM_FRAME_RE.match(ln)]
    summary = [ln for ln in keep if re.search(r"PATHS|SIZE|DIFF|built|error|warning|Finished|Caching|<<<|>>>", ln)]
    # Cap summary at tail lines, sliced from end so trailing errors survive.
    body = summary[-tail:] if summary else keep[-tail:]
    if len(keep) > len(body):
        body = [f"… ({len(keep) - len(body)} lines elided; pass logs=true for full output)"] + body
    return "\n".join(body)


class CommandTimeout(TimeoutError):
    """Timeout that may carry streamed output captured before the kill."""

    partial_output: str  # pyright: ignore[reportUninitializedInstanceVariable]  # set only on the streaming path; tests require absence otherwise


async def _communicate(proc: asyncio.subprocess.Process, timeout: float) -> tuple[int, str, str]:
    """Await a subprocess, killing its process group if the timeout is hit."""
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        _kill_proc_group(proc)
        try:
            await asyncio.wait_for(proc.communicate(), 5)
        except Exception:
            pass
        raise CommandTimeout(f"command timed out after {timeout}s; process group signalled, elevated (sudo/root) descendants may still be running") from None
    finally:
        _kill_proc_group(proc, only_if_running=True)
    assert proc.returncode is not None  # communicate() completed
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def run_icedos(*args: str, timeout: float = 300) -> tuple[int, str, str]:
    """Run an icedos CLI command and return (returncode, stdout, stderr)."""
    bin_path = _find_icedos_bin()
    config_root = find_config_root()

    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config_root),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
        start_new_session=True,
    )
    return await _communicate(proc, timeout)


async def run_icedos_streaming(
    *args: str,
    timeout: float = 300,
    on_line=None,
) -> tuple[int, str, str]:
    """Like run_icedos, but stream stdout line-by-line to on_line as it arrives.

    stderr is merged into stdout so the log stays in order and a single stream can
    be pumped; the returned stderr is always empty. on_line is an async callback
    invoked with each decoded line (its failures must not abort the build — the
    caller is responsible for swallowing them).
    """
    bin_path = _find_icedos_bin()
    config_root = find_config_root()

    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(config_root),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
        start_new_session=True,
    )

    lines: list[str] = []

    async def _pump() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # Lines exceeding StreamReader limit raise ValueError (chunk discarded) — keep pumping.
                continue
            if not raw:
                break
            line = raw.decode(errors="replace")
            lines.append(line)
            if on_line is not None:
                await on_line(line)
        await proc.wait()

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        _kill_proc_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            pass
        err = CommandTimeout(
            f"command timed out after {timeout}s; process group signalled, "
            "elevated (sudo/root) descendants may still be running"
        )
        err.partial_output = "".join(lines)
        raise err
    finally:
        _kill_proc_group(proc, only_if_running=True)
    assert proc.returncode is not None  # _pump() waited for exit
    return proc.returncode, "".join(lines), ""


async def rebuild(*flags: str, timeout: float = 3600, on_line=None) -> tuple[int, str, str]:
    """Run `icedos rebuild` with given flags. Always includes --build for safety.

    When on_line is given the build is streamed (each log line is passed to the
    callback as it arrives); otherwise the output is buffered until the process exits.
    """
    if on_line is not None:
        return await run_icedos_streaming("rebuild", "--build", *flags, timeout=timeout, on_line=on_line)
    return await run_icedos("rebuild", "--build", *flags, timeout=timeout)


async def get_diff() -> tuple[int, str, str]:
    """Run `icedos configuration diff`."""
    return await run_icedos("configuration", "diff")


async def rollback(
    generation: int | None = None, dry: bool = False
) -> tuple[int, str, str]:
    """Run `icedos configuration rollback`."""
    args = ["configuration", "rollback"]
    if generation is not None:
        args.extend(["--to", str(generation)])
    if dry:
        args.append("--dry")
    return await run_icedos(*args)


async def gc(
    days: int | None = None,
    generations: int | None = None,
    dry: bool = False,
    timeout: float = 1800,
) -> tuple[int, str, str]:
    """Run `icedos gc` with optional --days, --gens, --dry."""
    args = ["gc"]
    if days is not None:
        args.extend(["--days", str(days)])
    if generations is not None:
        args.extend(["--gens", str(generations)])
    if dry:
        args.append("--dry")
    return await run_icedos(*args, timeout=timeout)


async def doctor() -> tuple[int, str, str]:
    """Run `icedos status` (dashboard + health checks)."""
    return await run_icedos("status")


async def list_packages() -> tuple[int, str, str]:
    """Run `icedos pkgs list`."""
    return await run_icedos("pkgs", "list")


async def run_package(attr: str, timeout: float = 300) -> tuple[int, str, str]:
    """Run `icedos pkgs run <attr>`."""
    return await run_icedos("pkgs", "run", attr, timeout=timeout)


async def _run_build_sh(*flags: str, timeout: float = 180) -> tuple[int, str, str]:
    """Run the generated `.state/build.sh` with flags (no closure realize)."""
    config_root = find_config_root()
    state_dir = config_root / ".state"
    build_sh = state_dir / "build.sh"

    if not build_sh.exists():
        raise FileNotFoundError(
            f"build.sh not found at {build_sh}. Run 'icedos rebuild' first."
        )

    proc = await asyncio.create_subprocess_exec(
        "bash", str(build_sh), *flags,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(state_dir),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
        start_new_session=True,
    )
    return await _communicate(proc, timeout)


async def export_search_index() -> tuple[int, str, str]:
    """Run build.sh --export-search-index to regenerate option/module docs."""
    return await _run_build_sh("--export-search-index", timeout=120)


async def genflake_only(timeout: float = 180) -> tuple[int, str, str]:
    """Evaluate + lock the config without realizing a closure.

    Catches schema/type/duplicate-key errors far cheaper than a full build —
    used as the fast validation path for config writes.
    """
    return await _run_build_sh("--genflake-only", timeout=timeout)


# Called directly (not via icedos wrapper), so flake features must be requested explicitly.
_NIX_EVAL_FEATURES = "experimental-features = flakes nix-command pipe-operators"


def _nix_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("NIX_CONFIG", "").strip()
    env["NIX_CONFIG"] = f"{existing}\n{_NIX_EVAL_FEATURES}" if existing else _NIX_EVAL_FEATURES
    return env


def _nix_string(value: object) -> str:
    """Quote a Python value as a Nix string literal.

    `${` opens an interpolation inside a Nix string, so it needs escaping next to
    the usual quote/backslash pair — a config path containing either would
    otherwise silently change the meaning of the generated expression.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def _nix_eval_prelude(config_root: Path) -> str:
    """A ``let ... in`` header binding core's shared REPL context.

    The bindings come from `core/lib/repl-context.nix` — the same file backing
    `icedos repl`, so both surfaces agree on what `config` and `pkgs` mean. The
    core root is resolved *inside* Nix, from the generated flake's own
    `icedos-core` input, which works for a `github:` pin exactly as it does for a
    local `path:` override.
    """
    state_dir = config_root / ".state"
    if not (state_dir / "flake.nix").exists():
        raise FileNotFoundError(
            f"no generated flake at '{state_dir}'; run 'rebuild' once first."
        )

    return (
        "let\n"
        f"  stateDir = {_nix_string(state_dir)};\n"
        f"  configRoot = {_nix_string(config_root)};\n"
        '  core = (builtins.getFlake "path:${stateDir}").inputs.icedos-core;\n'
        '  ctx = import "${core}/lib/repl-context.nix" { inherit configRoot stateDir; };\n'
        "  inherit (ctx) config options declared pkgs lib icedosLib inputs;\n"
        "in\n"
    )


async def nix_eval_expr(
    expression: str, *, timeout: float = 120, raw: bool = False
) -> tuple[int, str, str]:
    """Evaluate a Nix expression against the IceDOS REPL context.

    Impure by necessity: the context reads the generated flake and the
    working-tree config. ``raw`` swaps ``--json`` for ``--raw`` (strings only).
    """
    config_root = find_config_root()
    full_expr = _nix_eval_prelude(config_root) + expression

    proc = await asyncio.create_subprocess_exec(
        "nix", "eval", "--impure", "--raw" if raw else "--json", "--expr", full_expr,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config_root),
        env=_nix_env(),
        start_new_session=True,
    )
    return await _communicate(proc, timeout)
