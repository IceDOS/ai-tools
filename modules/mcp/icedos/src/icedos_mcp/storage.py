"""Secondary drives: pick one for heavy files or a Nix store, and report what ai-dump holds."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from . import settings as settings_file

AI_DUMP = "ai-dump"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def settings() -> dict[str, Any]:
    return settings_file.load()["storage"]


def store_dir(mount: str) -> str:
    return str(Path(mount) / AI_DUMP / "nix-store")


def _space(path: str) -> tuple[float, float]:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2**30, st.f_blocks * st.f_frsize / 2**30


def _row(mount: str, io: str, reserve: float) -> dict[str, Any]:
    if not os.path.ismount(mount):
        return {"mount": mount, "io": io, "mounted": False}
    free, total = _space(mount)
    return {
        "mount": mount, "io": io, "mounted": True,
        "free_gb": round(free, 1), "total_gb": round(total, 1),
        # Space a task may use without eating into the reserve.
        "room_gb": round(free - reserve, 1),
        "nix_store": Path(store_dir(mount)).is_dir(),
    }


def root() -> dict[str, Any]:
    row = _row("/", "root", float(settings()["reserveGb"]))
    row.pop("nix_store", None)
    return row


def drives() -> list[dict[str, Any]]:
    cfg = settings()
    return [_row(m, io, float(cfg["reserveGb"])) for m, io in cfg["drives"].items()]


def pick(need_gb: float, io: str = "random", project: str = "") -> dict[str, Any]:
    """Drive with room for need_gb above the reserve, preferring the asked I/O kind, then most room."""
    if io not in ("random", "sequential"):
        raise ValueError("io must be 'random' or 'sequential'")
    if project and not _NAME_RE.match(project):
        raise ValueError("project must be letters, digits, '.', '_' or '-', not starting with '.' or '-'")
    rows = [r for r in drives() if r["mounted"] and r["room_gb"] >= need_gb]
    if not rows:
        raise ValueError(f"no configured drive has {need_gb:g} GB free above the {settings()['reserveGb']} GB reserve")
    rows.sort(key=lambda r: (r["io"] != io, -r["room_gb"]))
    best = rows[0]
    out = {k: best[k] for k in ("mount", "io", "free_gb", "room_gb")}
    out["matches_io"] = best["io"] == io
    if project:
        d = Path(best["mount"]) / AI_DUMP / project
        d.mkdir(parents=True, exist_ok=True)
        out["dir"] = str(d)
    return out


def store_for(need_gb: float, choice: str = "auto") -> tuple[str, str]:
    """Store dir for a local build ("" is the host store) and why it was picked.

    `auto` keeps small builds on the host store, where their dependencies already are.
    A build of largeGb or more, or one that would push / below the reserve, goes to the
    SSD with room that already has a store, else the one with the most room.
    """
    cfg = settings()
    rows = [r for r in drives() if r["mounted"]]
    if choice == "host":
        return "", "host store asked for"
    if choice != "auto":
        row = next((r for r in rows if r["mount"] == choice.rstrip("/")), None)
        if row is None:
            known = ", ".join(cfg["drives"]) or "none"
            raise ValueError(f"store must be auto, host or a mounted configured drive ({known})")
        return _prepare(row["mount"]), f"store on {row['mount']} asked for"
    reserve = float(cfg["reserveGb"])
    root_ok = _space("/")[0] - need_gb >= reserve
    large = need_gb >= float(cfg["largeGb"])
    if root_ok and not large:
        return "", "small build, host store has room"
    ssds = sorted(
        (r for r in rows if r["io"] == "random" and r["room_gb"] >= need_gb),
        key=lambda r: (not r["nix_store"], -r["room_gb"]),
    )
    if ssds:
        why = f"{need_gb:g} GB build" if large else f"/ would drop below the {reserve:g} GB reserve"
        return _prepare(ssds[0]["mount"]), f"{why}; store on {ssds[0]['mount']}"
    if root_ok:
        return "", "no SSD has room, host store has"
    raise ValueError(f"no SSD and not / has {need_gb:g} GB free above the {reserve:g} GB reserve")


def _prepare(mount: str) -> str:
    (Path(mount) / AI_DUMP).mkdir(exist_ok=True)
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
        size, _, name = line.partition("\t")
        if size.isdigit():
            sizes[Path(name).name] = int(size)
    return sizes


async def status() -> dict[str, Any]:
    """Free space of / and every drive, and the size of each ai-dump entry."""
    rows = drives()

    async def dump(r: dict[str, Any]) -> None:
        d = Path(r["mount"]) / AI_DUMP
        if r["mounted"] and d.is_dir() and any(d.iterdir()):
            r["ai_dump_mb"] = await _du(d)

    await asyncio.gather(*(dump(r) for r in rows))
    return {"reserve_gb": settings()["reserveGb"], "root": root(), "drives": rows}
