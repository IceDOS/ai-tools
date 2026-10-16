"""Nix leftovers: what a local flake copies into the store, and the GC roots agents leave behind."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import settings as settings_file
from .build_hosts import _delete, _full, gc_summary

HOST_STORE = "auto"
HEAVY_NAMES = {"node_modules", ".direnv", "target", "dist", "build", ".venv", "venv", "__pycache__", ".cache"}
HEAVY_SUFFIXES = (".img", ".qcow2", ".iso", ".raw", ".vdi", ".vmdk", ".tar", ".zst", ".gz", ".xz", ".zip")
_ROOT_RE = re.compile(r'^"?(?P<link>.+?)"? -> (?P<target>/nix/store/\S+)$')


def settings() -> dict[str, Any]:
    return settings_file.load()["storage"]


def flake_dir(installable: str) -> tuple[str, str] | None:
    """(dir, scheme) of a local flake installable, or None for a remote flake or store path.

    scheme is "path", "git" or "" when Nix picks by whether the dir is in a git repo.
    Raises ValueError for a relative path, since the server's working dir is not the caller's.
    """
    ref = installable.split("#", 1)[0]
    if ref.startswith("path:"):
        d, scheme = unquote(urlsplit(ref).path or ref[5:].split("?", 1)[0]), "path"
    elif ref.startswith("git+file:"):
        d, scheme = unquote(urlsplit(ref).path), "git"
    elif ref.startswith(("/", ".", "~")) and not ref.startswith("/nix/store/"):
        d, scheme = ref.split("?", 1)[0], ""
    else:
        return None
    if not d.startswith("/"):
        raise ValueError(f"{installable!r} is relative; pass an absolute path, the server's working dir is not yours")
    return d, scheme


def _git_top(d: str) -> str:
    proc = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _du_mb(paths: list[str], timeout: float = 60) -> dict[str, int] | None:
    """Apparent size in MB per path; None when du runs past the timeout."""
    if not paths:
        return {}
    try:
        proc = subprocess.run(
            ["du", "-sm", "--apparent-size", *paths], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None
    sizes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        size, _, name = line.partition("\t")
        if size.isdigit():
            sizes[name] = int(size)
    return sizes


def preflight(installable: str) -> dict[str, Any] | None:
    """What evaluating a local flake copies into the store; None when the installable is not local.

    A path flake copies the whole dir, .gitignore'd files and .git included. A git flake
    copies the tracked files of the whole repo, and new files stay invisible until `git add`.
    """
    found = flake_dir(installable)
    if found is None:
        return None
    d, scheme = found
    if not Path(d, "flake.nix").is_file():
        return {"dir": d, "ok": False, "verdict": "refuse", "reason": f"no flake.nix in {d}"}
    top = _git_top(d)
    mode = scheme or ("git" if top else "path")
    limit = int(settings()["maxFlakeCopyMb"])
    out: dict[str, Any] = {"dir": d, "mode": mode, "limit_mb": limit}
    if mode == "path":
        entries = sorted(str(p) for p in Path(d).iterdir() if not p.is_symlink())
        sizes = _du_mb(entries)
        if sizes is None:
            out.update(verdict="refuse", reason="du ran past a minute; the dir is too big to copy")
            return out
        out["copy_mb"] = sum(sizes.values())
        big = sorted(((n, s) for n, s in sizes.items() if s >= 50), key=lambda x: -x[1])
        heavy = [n for n in entries if Path(n).name in HEAVY_NAMES or n.endswith(HEAVY_SUFFIXES)]
        out["largest"] = [{"path": n, "mb": s} for n, s in big[:8]]
        out["heavy"] = heavy
        if top:
            out["note"] = f"{d} is in the git repo {top}, but path: ignores git and copies everything"
        fix = (
            "run `git init`, add a .gitignore for the heavy entries and `git add` the needed files, "
            "or move the flake into a small subdirectory"
        )
    else:
        root = top or d
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=60)
        files = [f for f in proc.stdout.decode(errors="replace").split("\0") if f]
        total = 0
        for f in files:
            try:
                total += os.lstat(Path(root, f)).st_size
            except OSError:
                pass
        out["copy_mb"] = total // 2**20
        out["repo"] = root
        proc = subprocess.run(
            ["git", "-C", d, "ls-files", "-z", "--others", "--exclude-standard", "."], capture_output=True, timeout=60
        )
        untracked = [f for f in proc.stdout.decode(errors="replace").split("\0") if f]
        if untracked:
            out["untracked"] = untracked[:20]
            out["untracked_note"] = f"{len(untracked)} untracked files are invisible to the flake until `git add`"
        fix = "add .gitignore rules or `git rm --cached` the heavy tracked files, or move the flake into a small repo"
    if out["copy_mb"] > limit:
        out.update(verdict="refuse", reason=f"copies {out['copy_mb']} MB into the store, over {limit} MB; {fix}")
    else:
        out.update(verdict="ok", reason=f"copies {out['copy_mb']} MB")
    out["ok"] = out["verdict"] == "ok"
    return out


def source_path(installable: str, store: str = "") -> str:
    """Store path of a local flake's source copy, or "" when Nix cannot say."""
    ref = installable.split("#", 1)[0]
    flags = ["--store", store] if store else []
    try:
        proc = subprocess.run(
            ["nix", "flake", "metadata", "--json", "--no-write-lock-file", *flags, ref],
            capture_output=True, text=True, timeout=300,
        )
        return json.loads(proc.stdout).get("path", "") if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, ValueError):
        return ""


def _skip_root(link: str, home: str) -> bool:
    """System and profile roots, and Nix's own caches, never count as leftovers."""
    if link.startswith(("/proc/", "/nix/var/", "/run/", "/root/", "{")):
        return True
    if "/.cache/nix/" in link:
        return True
    return link.startswith(tuple(f"{home}/{p}" for p in (
        ".nix-profile", ".local/state/nix/", ".local/state/home-manager/", ".nix-defexpr/",
    )))


def removable(link: str) -> str:
    """Why a root may not go, or "" when it may: only roots under home, /tmp or an ai-dump dir."""
    home = str(Path.home())
    if _skip_root(link, home):
        return "system, profile or Nix cache root"
    if link.startswith(("/tmp/", f"{home}/")) or "/ai-dump/" in link:
        return ""
    return "outside home, /tmp and ai-dump"


def _kind(link: str) -> str:
    name = Path(link).name
    if "/.direnv/" in link:
        return "direnv"
    if name.startswith("result"):
        return "result"
    if link.startswith("/tmp/") or "/tmp/" in link:
        return "temp"
    return "other"


def roots(under: str = "", sizes: bool = True) -> list[dict[str, Any]]:
    """GC roots outside system, profile and cache roots, optionally only those under a dir."""
    proc = subprocess.run(["nix-store", "--gc", "--print-roots"], capture_output=True, text=True, timeout=300)
    home = str(Path.home())
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        m = _ROOT_RE.match(line.strip())
        if not m or _skip_root(m["link"], home):
            continue
        link = m["link"]
        if under and not link.startswith(under.rstrip("/") + "/"):
            continue
        why = removable(link)
        rows.append({
            "link": link, "target": m["target"], "kind": _kind(link),
            "removable": not why, **({"keep_reason": why} if why else {}),
        })
    if sizes and rows:
        targets = sorted({r["target"] for r in rows})
        info = subprocess.run(
            ["nix", "path-info", "--json", "-S", *targets], capture_output=True, text=True, timeout=300
        )
        try:
            data = json.loads(info.stdout) if info.returncode == 0 else {}
        except ValueError:
            data = {}
        data = data.get("info", data) if isinstance(data, dict) else {}
        data = {_full(k): v for k, v in data.items()}
        for r in rows:
            if (d := data.get(r["target"])) and d.get("closureSize"):
                r["closure_mb"] = d["closureSize"] // 2**20
    return rows


def remove(links: list[str], delete: bool = True, timeout: float = 300) -> dict[str, Any]:
    """Unlink roots that `roots` lists as removable, then delete their targets unless still alive."""
    listed = {r["link"]: r for r in roots(sizes=False)}
    report: dict[str, Any] = {"removed": [], "refused": {}}
    targets: set[str] = set()
    for link in links:
        row = listed.get(link)
        if row is None:
            report["refused"][link] = "not a GC root outside system, profile and cache roots"
        elif not row["removable"]:
            report["refused"][link] = row["keep_reason"]
        elif not os.path.islink(link):
            report["refused"][link] = "not a symlink"
        else:
            os.unlink(link)
            report["removed"].append(link)
            targets.add(row["target"])
    if delete and targets:
        # A target another root still holds stays; so does one something running uses.
        still = {r["target"] for r in roots(sizes=False)}
        gone, alive, error = _delete(HOST_STORE, targets - still, timeout)
        report["deleted"] = sorted(gone)
        if alive or targets & still:
            report["kept_live"] = sorted(alive | (targets & still))
        if error:
            report["error"] = error
    return report


def collect(timeout: float = 1800) -> str:
    """`nix store gc` on the host store. Never removes generations, so rollbacks survive."""
    proc = subprocess.run(["nix", "store", "gc"], capture_output=True, text=True, timeout=timeout)
    return gc_summary(proc)
