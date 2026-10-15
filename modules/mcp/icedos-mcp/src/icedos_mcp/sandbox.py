"""Throwaway copies of the config root that agents can edit and build in.

A sandbox is a plain directory copy of the live config root. Tools run against
it through ``config.use_config_root``, and ``rebuild`` builds it with
``icedos rebuild --dir <sandbox> --build``, so the live config, its ``.state``
and the system profile are never touched. Each agent can own a sandbox, so many
agents can work at once without clobbering each other.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

from . import config_files
from .config import find_config_root

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Never copied: VCS data, build links and live state (re-created per sandbox below).
_SKIP_TOP = {".git", ".state"}

# .state files worth carrying over: the pinned lock (so the build uses the live
# input revs) and the search index (so option/module queries work before a build).
_STATE_FILES = ("flake.nix", "flake.lock", ".gitignore")
_CACHE_FILES = ("options-doc.json", "modules-doc.json", "config.json")


def sandboxes_dir() -> Path:
    """Parent dir of all sandboxes; `ICEDOS_SANDBOX_DIR` overrides the tmp default.

    Created 0700 on first use. An existing dir must be a real dir owned by this
    user and closed to others, since a shared /tmp lets anyone pre-create it.
    """
    raw = os.environ.get("ICEDOS_SANDBOX_DIR")
    base = Path(raw) if raw else Path(tempfile.gettempdir()) / f"icedos-sandboxes-{os.getuid()}"
    try:
        base.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = base.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(
            f"{base} must be a directory owned by uid {os.getuid()} with mode 0700 (not a symlink)"
        )
    return base


def _meta_dir() -> Path:
    # Sandbox names never start with '.', so this cannot clash with one.
    d = sandboxes_dir() / ".meta"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _meta_path(name: str) -> Path:
    return _meta_dir() / f"{name}.json"


def _base_path(name: str) -> Path:
    """Pristine copy of the live config taken at create time, the left side of diffs."""
    return _meta_dir() / f"{name}.base"


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid sandbox name '{name}': use letters, digits, '.', '_' or '-' (max 64)"
        )
    return name


def resolve(name: str) -> Path:
    """Path of an existing sandbox."""
    path = sandboxes_dir() / _check_name(name)
    if not (path / "flake.nix").is_file():
        raise FileNotFoundError(f"no sandbox named '{name}' (see sandbox_list)")
    return path


def _ignore_top(root: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory) != root:
            return {n for n in names if n == config_files.OVERRIDES_FILE}
        return {
            n
            for n in names
            if n in _SKIP_TOP or n == "result" or n.startswith("result-") or n == config_files.OVERRIDES_FILE
        }

    return ignore


def _write_build_sh(state: Path, root: Path) -> None:
    # Same shape as the one core's flake app writes, pointed at the sandbox.
    _ = (state / "build.sh").write_text(
        f'#!/usr/bin/env bash\nset -e\ncd "{root}"\nnix run path:. -- "$@"\n'
    )


def create(name: str = "") -> dict[str, Any]:
    """Copy the live config root into a new sandbox and return its metadata."""
    live = find_config_root()
    name = _check_name(name) if name else f"sb-{secrets.token_hex(3)}"
    dest = sandboxes_dir() / name
    if dest.exists():
        raise FileExistsError(f"sandbox '{name}' already exists")

    try:
        _ = shutil.copytree(live, dest, symlinks=True, ignore=_ignore_top(live))
        _ = shutil.copytree(dest, _base_path(name), symlinks=True)
        state = dest / ".state"
        (state / ".cache").mkdir(parents=True)
        for rel in _STATE_FILES:
            src = live / ".state" / rel
            if src.is_file():
                _ = shutil.copy2(src, state / rel)
        for rel in _CACHE_FILES:
            src = live / ".state" / ".cache" / rel
            if src.is_file():
                _ = shutil.copy2(src, state / ".cache" / rel)
        _write_build_sh(state, dest)

        meta = {"name": name, "path": str(dest), "source": str(live), "created": int(time.time())}
        _ = _meta_path(name).write_text(json.dumps(meta, indent=2))
    except BaseException:
        _remove(name, dest)
        raise
    return meta


def _remove(name: str, path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(_base_path(name), ignore_errors=True)
    _meta_path(name).unlink(missing_ok=True)


def _meta(name: str) -> dict[str, Any]:
    try:
        return json.loads(_meta_path(name).read_text())
    except (OSError, ValueError):
        return {}


def list_all() -> list[dict[str, Any]]:
    base = sandboxes_dir()
    out = []
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir() or not (entry / "flake.nix").is_file():
            continue
        meta: dict[str, Any] = {"name": entry.name, "path": str(entry), **_meta(entry.name)}
        try:
            meta["overrides"] = read_overrides(entry)
        except (OSError, ValueError, KeyError):
            meta["overrides"] = {}
        out.append(meta)
    return out


def delete(name: str) -> None:
    _remove(name, resolve(name))


def _overrides_path(root: Path) -> Path:
    dirs = config_files.extra_config_dirs(root)
    if not dirs:
        raise ValueError("icedos.system.extraConfigs is empty, so core loads no file the overrides could go in")
    return root / dirs[0] / config_files.OVERRIDES_FILE


def read_overrides(root: Path) -> dict[str, str]:
    """repo url -> overrideUrl set through set_override in this sandbox."""
    path = _overrides_path(root)
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text())
    return {r["url"]: r["overrideUrl"] for r in data.get("icedos", {}).get("repositories", [])}


def _configured_urls(root: Path) -> list[str]:
    """Exact urls of every [[icedos.repositories]] entry in the user's config files."""
    urls: list[str] = []
    for f in config_files.writable_files(root):
        for r in config_files._read(f).get("icedos", {}).get("repositories", []) or []:
            if r.get("url") and r["url"] not in urls:
                urls.append(r["url"])
    return urls


def _resolve_repo_urls(root: Path, repo: str) -> list[str]:
    """Configured urls for ``repo``: a flake url (matched by base url) or a repo name.

    Core keys overrides by exact url, so a repo listed with two refs yields both.
    Only configured repos qualify; an override entry for any other url would add
    that repo to the build.
    """
    urls = _configured_urls(root)
    if ":" in repo:
        want = config_files.base_url(repo)
        matches = [u for u in urls if config_files.base_url(u) == want]
        if not matches:
            raise ValueError(f"'{repo}' is not a configured repository; add it with add_repository first")
        return matches
    names = {config_files.base_url(u).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] for u in urls}
    bases = sorted(
        {config_files.base_url(u) for u in urls if config_files.base_url(u).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] == repo}
    )
    if len(bases) != 1:
        raise ValueError(
            f"'{repo}' matches {len(bases)} configured repos{': ' + ', '.join(bases) if bases else ''}; "
            f"pass the full url (known names: {', '.join(sorted(names))})"
        )
    return [u for u in urls if config_files.base_url(u) == bases[0]]


def _local_flake_url(path: str) -> str:
    raw = path.removeprefix("path:")
    local = Path(raw).expanduser().resolve()
    if not (local / "flake.nix").is_file():
        raise FileNotFoundError(f"'{local}' has no flake.nix, so it is not an icedos repo checkout")
    return f"path:{local}"


def set_override(root: Path, repo: str, path: str) -> dict[str, Any]:
    """Point `repo` at a local checkout in this sandbox, or clear it when `path` is empty.

    The overrides go in their own config file inside the sandbox, so no user
    config file changes. Core takes the first overrideUrl it sees for a url, so
    one set by a file that loads earlier still wins; the result warns about it.
    """
    urls = _resolve_repo_urls(root, repo)
    overrides = read_overrides(root)
    for url in urls:
        if path:
            overrides[url] = _local_flake_url(path)
        else:
            _ = overrides.pop(url, None)

    target = _overrides_path(root)
    if overrides:
        lines = ["# Managed by the icedos MCP (sandbox_override_repo). Left out of sandbox diffs.\n"]
        for u, o in sorted(overrides.items()):
            # JSON string escapes are valid TOML basic-string escapes.
            lines.append(f"\n[[icedos.repositories]]\nurl = {json.dumps(u)}\noverrideUrl = {json.dumps(o)}\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text("".join(lines))
    else:
        target.unlink(missing_ok=True)

    warnings = []
    for f in config_files.list_config_files(root):
        if f == target:
            break
        for r in config_files._read(f).get("icedos", {}).get("repositories", []) or []:
            if r.get("url") in urls and r.get("overrideUrl"):
                rel = f.relative_to(root).as_posix()
                warnings.append(f"{rel} sets overrideUrl {r['overrideUrl']} for {r['url']} and loads first, so it wins")
    return {"urls": urls, "overrides": overrides, "warnings": warnings}


def _tracked_files(root: Path) -> set[str]:
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root:
            dirnames[:] = [d for d in dirnames if d not in _SKIP_TOP]
            filenames = [f for f in filenames if f != "result" and not f.startswith("result-")]
        filenames = [f for f in filenames if f != config_files.OVERRIDES_FILE]
        out.update(str((here / f).relative_to(root)) for f in filenames)
    return out


def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text().splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def _same(a: Path, b: Path) -> bool:
    if a.is_file() != b.is_file():
        return False
    return not a.is_file() or a.read_bytes() == b.read_bytes()


def _unified(left: Path, right: Path) -> str:
    chunks: list[str] = []
    for rel in sorted(_tracked_files(left) | _tracked_files(right)):
        a, b = left / rel, right / rel
        if _same(a, b):
            continue
        a_lines = _lines(a) if a.is_file() else []
        b_lines = _lines(b) if b.is_file() else []
        if a_lines is None or b_lines is None:
            chunks.append(f"Binary files a/{rel} and b/{rel} differ\n")
            continue
        for line in difflib.unified_diff(
            a_lines,
            b_lines,
            fromfile=f"a/{rel}" if a.is_file() else "/dev/null",
            tofile=f"b/{rel}" if b.is_file() else "/dev/null",
        ):
            chunks.append(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n")
    return "".join(chunks)


def diff(name: str) -> str:
    """Unified diff of the sandbox's changes: the config as copied (a/) against the sandbox (b/).

    The left side is the snapshot taken at create time, so edits made to the
    live config later never show up as reverse hunks. A sandbox made before
    snapshots existed falls back to the live config.
    """
    sandbox = resolve(name)
    base = _base_path(name)
    if not base.is_dir():
        base = Path(_meta(name).get("source") or find_config_root())
    return _unified(base, sandbox)


def live_drift(name: str) -> list[str]:
    """Config files the live config changed since the sandbox was copied from it."""
    base = _base_path(name)
    source = _meta(name).get("source")
    if not base.is_dir() or not source or not Path(source).is_dir():
        return []
    live = Path(source)
    return sorted(rel for rel in _tracked_files(base) | _tracked_files(live) if not _same(base / rel, live / rel))
