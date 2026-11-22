"""Secondary drives: pick one for heavy files or a Nix store, and report what ai-dump holds."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import settings as settings_file

AI_DUMP = "ai-dump"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DF_RE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+(.*)$")
_GB = 1024 * 1024


def settings(host: str = "local") -> dict[str, Any]:
    return settings_file.storage(host)


def store_dir(mount: str) -> str:
    return str(Path(mount) / AI_DUMP / "nix-store")


def _space(path: str) -> tuple[float, float]:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2**30, st.f_blocks * st.f_frsize / 2**30


def _row(mount: str, io: str, reserve: float, info: tuple[float, float, bool] | None) -> dict[str, Any]:
    """One drive row; info is (free, total, has store) in GB, None when the mount is not there."""
    if info is None:
        return {"mount": mount, "io": io, "mounted": False}
    free, total, has_store = info
    return {
        "mount": mount, "io": io, "mounted": True,
        "free_gb": round(free, 1), "total_gb": round(total, 1),
        # Space a task may use without eating into the reserve.
        "room_gb": round(free - reserve, 1),
        "nix_store": has_store,
    }


def _local_row(mount: str, io: str, reserve: float) -> dict[str, Any]:
    if not os.path.ismount(mount):
        return {"mount": mount, "io": io, "mounted": False}
    free, total = _space(mount)
    return _row(mount, io, reserve, (free, total, Path(store_dir(mount)).is_dir()))


def _ssh(host: str, script: str, timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["ssh", *settings_file.SSH_OPTS, host, script], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def _host_probe(host: str, mounts: list[str]) -> tuple[tuple[float, float], dict[str, tuple[float, float, bool] | None]]:
    """Free and total GB of /nix/store and of each mount on host, and whether it holds a store; one ssh call."""
    script = 'line=$(df -Pk /nix/store 2>/dev/null | tail -n 1); echo "@@root 0 $line"'
    for i, m in enumerate(mounts):
        q = shlex.quote(m)
        q_store = shlex.quote(f"{m}/ai-dump/nix-store")
        script += (
            f'; line=$(df -Pk {q} 2>/dev/null | tail -n 1); n=0; '
            f'[ -d {q_store} ] && n=1; echo "@@{i} $n $line"'
        )
    rc, out = _ssh(host, script)
    if rc != 0:
        raise ValueError(f"cannot read the drives of {host}: {out.strip()[-300:]}")
    root: tuple[float, float] | None = None
    info: dict[str, tuple[float, float, bool] | None] = {m: None for m in mounts}
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        tag, _, rest = line.partition(" ")
        has_store_str, _, df_line = rest.partition(" ")
        has_store = has_store_str == "1"
        m_df = _DF_RE.search(df_line.strip())
        if not m_df:
            continue
        total, used, avail, mounted_on = m_df.groups()
        total_kb, avail_kb = int(total), int(avail)
        if tag == "@@root":
            root = (avail_kb / _GB, total_kb / _GB)
        elif tag.startswith("@@"):
            idx_str = tag[2:]
            if idx_str.isdigit():
                idx = int(idx_str)
                if 0 <= idx < len(mounts):
                    mount = mounts[idx]
                    expected = mount.rstrip("/") or "/"
                    if (mounted_on.rstrip("/") or "/") == expected:
                        info[mount] = (avail_kb / _GB, total_kb / _GB, has_store)
                    else:
                        info[mount] = None
    if root is None:
        raise ValueError(f"cannot parse df output of {host}: {out.strip()[-300:]}")
    return root, info


def _host_state(host: str, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drive rows and the /nix/store row of a host, from one probe."""
    root_info, info = _host_probe(host, list(cfg["drives"]))
    reserve = float(cfg["reserveGb"])
    rows = [_row(m, io, reserve, info.get(m)) for m, io in cfg["drives"].items()]
    root_row = _row("/nix/store", "root", reserve, (*root_info, False))
    root_row.pop("nix_store", None)
    return rows, root_row


def _mkdir(host: str, path: str) -> None:
    if host == "local":
        Path(path).mkdir(parents=True, exist_ok=True)
        return
    rc, out = _ssh(host, f"mkdir -p {shlex.quote(path)}")
    if rc != 0:
        raise ValueError(f"cannot create {path} on {host}: {out.strip()[-300:]}")


def root(host: str = "local") -> dict[str, Any]:
    if host != "local":
        return _host_state(host, settings(host))[1]
    row = _local_row("/", "root", float(settings()["reserveGb"]))
    row.pop("nix_store", None)
    return row


HOST = "host"


def rank(host: str = "local") -> dict[str, int]:
    """Position of each mount, and "host" for the host store, in storage.priority.

    Unlisted mounts share the slot after the list. Without a "host" entry the host store comes first.
    """
    order = [m.rstrip("/") or "/" for m in settings(host)["priority"]]
    if HOST not in order:
        order.insert(0, HOST)
    return {m: i for i, m in reversed(list(enumerate(order)))}


def _rank_of(ranks: dict[str, int], mount: str) -> int:
    return ranks.get(mount.rstrip("/") or "/", len(ranks))


def drives(host: str = "local") -> list[dict[str, Any]]:
    cfg = settings(host)
    if host != "local":
        return _host_state(host, cfg)[0]
    return [_local_row(m, io, float(cfg["reserveGb"])) for m, io in cfg["drives"].items()]


def pick(need_gb: float, io: str = "random", project: str = "", host: str = "local") -> dict[str, Any]:
    """Drive with room for need_gb above the reserve, preferring the asked I/O kind, then priority, then most room.

    On a host the mount is a path there, and project creates its dir over ssh.
    """
    if io not in ("random", "sequential"):
        raise ValueError("io must be 'random' or 'sequential'")
    if project and not _NAME_RE.match(project):
        raise ValueError("project must be letters, digits, '.', '_' or '-', not starting with '.' or '-'")
    rows = [r for r in drives(host) if r["mounted"] and r["room_gb"] >= need_gb]
    if not rows:
        where = "" if host == "local" else f" on {host}"
        raise ValueError(f"no configured drive{where} has {need_gb:g} GB free above the "
                         f"{settings(host)['reserveGb']} GB reserve")
    ranks = rank(host)
    rows.sort(key=lambda r: (r["io"] != io, _rank_of(ranks, r["mount"]), -r["room_gb"]))
    best = rows[0]
    out = {k: best[k] for k in ("mount", "io", "free_gb", "room_gb")}
    out["matches_io"] = best["io"] == io
    if project:
        d = str(Path(best["mount"]) / AI_DUMP / project)
        _mkdir(host, d)
        out["dir"] = d
    return out


def store_for(need_gb: float, choice: str = "auto", host: str = "local") -> tuple[str, str]:
    """Store dir for a build ("" is the host store) and why it was picked.

    auto takes the first fit in storage.priority. The host store fits a build under largeGb
    that keeps the store above the reserve; an SSD fits when it has room.
    """
    cfg = settings(host)
    if host == "local":
        rows = [r for r in drives() if r["mounted"]]
        root_free = _space("/")[0]
    else:
        rows, root_row = _host_state(host, cfg)
        rows = [r for r in rows if r["mounted"]]
        root_free = root_row["free_gb"]
    if choice == "host":
        return "", "host store asked for"
    if choice != "auto":
        row = next((r for r in rows if r["mount"] == choice.rstrip("/")), None)
        if row is None:
            known = ", ".join(cfg["drives"]) or "none"
            raise ValueError(f"store must be auto, host or a mounted configured drive ({known})")
        return _prepare(host, row["mount"]), f"store on {row['mount']} asked for"
    reserve = float(cfg["reserveGb"])
    root_ok = root_free - need_gb >= reserve
    large = need_gb >= float(cfg["largeGb"])
    ranks = rank(host)
    ssds = sorted(
        (r for r in rows if r["io"] == "random" and r["room_gb"] >= need_gb),
        key=lambda r: (_rank_of(ranks, r["mount"]), not r["nix_store"], -r["room_gb"]),
    )
    host_fits = root_ok and not large
    if host_fits and (not ssds or ranks[HOST] < _rank_of(ranks, ssds[0]["mount"])):
        return "", "small build, host store has room"
    if ssds:
        where_root = "/nix/store" if host != "local" else "/"
        if host_fits:
            why = "drive comes before the host store in storage.priority"
        elif large:
            why = f"{need_gb:g} GB build"
        else:
            why = f"{where_root} would drop below the {reserve:g} GB reserve"
        return _prepare(host, ssds[0]["mount"]), f"{why}; store on {ssds[0]['mount']}"
    if root_ok:
        return "", "no SSD has room, host store has"
    where_root = "/nix/store" if host != "local" else "/"
    raise ValueError(f"no SSD and not {where_root} has {need_gb:g} GB free above the {reserve:g} GB reserve")


def _prepare(host: str, mount: str) -> str:
    _mkdir(host, str(Path(mount) / AI_DUMP))
    return store_dir(mount)


async def _du(path: Path) -> dict[str, int]:
    """Size in MB of each entry under path; empty when du fails or runs past a minute."""
    proc = await asyncio.create_subprocess_exec(
        "du", "-sm", *sorted(str(p) for p in path.iterdir()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return {}
    sizes: dict[str, int] = {}
    for line in out.decode(errors="replace").splitlines():
        size, _, name = line.partition("	")
        if size.isdigit():
            sizes[Path(name).name] = int(size)
    return sizes


def _host_du(host: str, mounts: list[str]) -> dict[str, dict[str, int]]:
    """Size in MB of each entry under a mount's ai-dump on host, in one ssh call."""
    if not mounts:
        return {}
    q = " ".join(shlex.quote(m) for m in mounts)
    script = (
        f'for m in {q}; do d="$m/ai-dump"; if [ -d "$d" ]; then '
        'echo "==$m"; du -sm "$d"/* 2>/dev/null || true; '
        'fi; done; true'
    )
    rc, out = _ssh(host, script, 60)
    if rc != 0:
        return {}
    sizes: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for line in out.splitlines():
        if line.startswith("=="):
            current = sizes.setdefault(line[2:], {})
            continue
        size, _, name = line.partition("	")
        if current is not None and size.isdigit():
            current[Path(name).name] = int(size)
    return sizes


def _host_status(host: str) -> tuple[dict[str, Any], tuple[list[dict[str, Any]], dict[str, Any]]]:
    cfg = settings(host)
    return cfg, _host_state(host, cfg)


async def status(host: str = "local") -> dict[str, Any]:
    """Free space of / and every drive, and the size of each ai-dump entry."""
    if host == "local":
        cfg = settings(host)
        rows, root_row = drives(), root()
    else:
        cfg, (rows, root_row) = await asyncio.to_thread(_host_status, host)

    async def dump(r: dict[str, Any]) -> None:
        d = Path(r["mount"]) / AI_DUMP
        if r["mounted"] and d.is_dir() and any(d.iterdir()):
            r["ai_dump_mb"] = await _du(d)

    if host == "local":
        await asyncio.gather(*(dump(r) for r in rows))
    else:
        sizes = await asyncio.to_thread(_host_du, host, [r["mount"] for r in rows if r["mounted"]])
        for r in rows:
            if sizes.get(r["mount"]):
                r["ai_dump_mb"] = sizes[r["mount"]]
    return {"reserve_gb": cfg["reserveGb"], "root": root_row, "drives": rows}
