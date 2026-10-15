"""Enumerate + load the user config file set.

Python mirror of ``core/lib/config/config-files.nix`` and
``load-user-config.nix`` — KEEP THEM IN SYNC. The load order (config.toml, then
every ``*.toml`` under the ``icedos.system.extraConfigs`` dirs recursively,
sorted by relative path), inclusion of hidden ``.*.toml``, the top-level
``enable = false`` opt-out, and the strict merge (tables recurse, lists
concatenate, the same scalar in two files is an error) must match core, or the
server's view will drift from what the system actually builds.

Reads use stdlib ``tomllib`` (plain dict/list) — format-preserving writes stay in
``config.py`` via tomlkit. Path helpers live in ``config_path.py`` so this
module stays import-safe (no top-level import of ``config``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

from .config_path import APPEND

# Sandbox-only file holding repo overrideUrls set through the MCP. Core loads it
# like any config file; edits never target it and sandbox diffs leave it out.
OVERRIDES_FILE = ".icedos-mcp-overrides.toml"


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        # Core's fromTOML aborts the whole eval on a broken file, so never skip it.
        raise ValueError(f"invalid TOML in '{path}': {e}") from None


def extra_config_dirs(root: Path) -> list[str]:
    """The ``icedos.system.extraConfigs`` dirs (default ``["configs"]``).

    Bootstrap value — read from ``config.toml`` only, mirroring core, never from
    the extra configs it selects. Each dir must stay inside the config root:
    core reads them from the flake's store copy, so anything else cannot load,
    and a sandbox must not reach outside its own copy.
    """
    main = root / "config.toml"
    if not main.is_file():
        return ["configs"]
    dirs = _read(main).get("icedos", {}).get("system", {}).get("extraConfigs", ["configs"])
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ValueError("icedos.system.extraConfigs must be a list of strings")
    root_r = root.resolve()
    for d in dirs:
        rel = Path(d)
        if rel.is_absolute() or ".." in rel.parts or not (root_r / rel).resolve().is_relative_to(root_r):
            raise ValueError(f"extraConfigs entry '{d}' must be a relative dir inside the config root")
    return list(dirs)


def _tomls_under(root: Path, d: str) -> list[Path]:
    """Every regular ``*.toml`` under ``root/d``, recursively, sorted by relative path like core."""
    found: list[tuple[str, Path]] = []
    for dirpath, _, names in os.walk(root / d):
        here = Path(dirpath)
        for n in names:
            p = here / n
            # core's readDir reports symlinks as "symlink", not "regular", so it skips them
            if n.endswith(".toml") and p.is_file() and not p.is_symlink():
                found.append((p.relative_to(root).as_posix(), p))
    return [p for _, p in sorted(found)]


def all_config_tomls(root: Path) -> list[Path]:
    """config.toml plus every extra ``*.toml``, disabled ones included."""
    main = root / "config.toml"
    files = [main] if main.is_file() else []
    for d in extra_config_dirs(root):
        files.extend(_tomls_under(root, d))
    return files


def list_config_files(root: Path) -> list[Path]:
    """Ordered config files that core loads: ``config.toml`` (optional, never
    gated) then every extra ``*.toml`` whose top-level ``enable`` is not ``false``."""
    files = []
    for p in all_config_tomls(root):
        if p.name == "config.toml" and p.parent == root or _read(p).get("enable") is not False:
            files.append(p)
    return files


def writable_files(root: Path) -> list[Path]:
    """list_config_files() minus the server-owned overrides file."""
    return [f for f in list_config_files(root) if f.name != OVERRIDES_FILE]


def _strip_enable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k != "enable"}


def _merge_strict(a: dict[str, Any], b: dict[str, Any], rel: str, path: list[str]) -> dict[str, Any]:
    """Port of core's mergeStrict: tables recurse, lists concatenate, a key set as a
    scalar (or with mismatched kinds) in two files is an error."""
    out = dict(a)
    for k, bv in b.items():
        if k not in a:
            out[k] = bv
            continue
        av = a[k]
        sub = [*path, k]
        if isinstance(av, dict) and isinstance(bv, dict):
            out[k] = _merge_strict(av, bv, rel, sub)
        elif isinstance(av, list) and isinstance(bv, list):
            out[k] = av + bv
        else:
            raise ValueError(
                f"duplicate config key '{'.'.join(sub)}' — set in '{rel}' and an earlier config file; define it in exactly one"
            )
    return out


def merged_config(root: Path) -> dict[str, Any]:
    """The merged config core builds from, or a ValueError where core would throw."""
    result: dict[str, Any] = {}
    for f in list_config_files(root):
        result = _merge_strict(result, _strip_enable(_read(f)), f.relative_to(root).as_posix(), [])
    return result


def resolve_file(root: Path, file: str | None) -> Path:
    """Absolute path for a ``file`` argument.

    ``None``/``""`` → ``config.toml``. Otherwise the arg must resolve to
    ``config.toml`` or a ``.toml`` anywhere under one of the ``extraConfigs``
    dirs, inside ``root`` (prevents writes outside the config tree). The path may
    not exist yet — creating a new ``configs/<name>.toml`` is allowed. The
    server-owned overrides file is never a valid target.
    """
    if not file:
        return root / "config.toml"
    root_r = root.resolve()
    p = (root / file).resolve()
    if not p.is_relative_to(root_r):
        raise ValueError(f"file '{file}' is outside the config root")
    if p == root_r / "config.toml":
        return p
    if p.suffix != ".toml":
        raise ValueError(f"file '{file}' must be a .toml file")
    if p.name == OVERRIDES_FILE:
        raise ValueError(f"'{file}' is managed by sandbox_override_repo")
    allowed = {(root_r / d).resolve() for d in extra_config_dirs(root)}
    if not any(p.is_relative_to(a) and p != a for a in allowed):
        raise ValueError(
            f"file '{file}' must be config.toml or a .toml under one of: "
            + ", ".join(sorted(str(a.relative_to(root_r)) for a in allowed))
        )
    return p


@dataclass
class Located:
    """Where a merged-view path lives on disk.

    ``files`` are the files (load order) holding the node the path names, all
    sharing the file-local tokens ``local``: merged ``[N]`` indices are mapped to
    one file's own index, which narrows ``files`` to that file. ``exists`` is
    False when the path is missing; ``files`` then holds the file that declares
    the deepest existing part of it (empty if none does).
    """

    files: list[Path] = field(default_factory=list)
    local: list[Any] = field(default_factory=list)
    exists: bool = False


def locate(root: Path, tokens: list[Any]) -> Located:
    nodes: list[tuple[Path, Any]] = [(f, _strip_enable(_read(f))) for f in writable_files(root)]
    local: list[Any] = []
    for i, tok in enumerate(tokens):
        if tok is APPEND:
            lists = [f for f, n in nodes if isinstance(n, list)]
            return Located(lists or [f for f, _ in nodes[:1]], [*local, APPEND], bool(lists))
        if isinstance(tok, str):
            nxt = [(f, n[tok]) for f, n in nodes if isinstance(n, dict) and tok in n]
            if not nxt:
                owner = [nodes[0][0]] if nodes and i > 0 else []
                return Located(owner, [*local, *tokens[i:]], False)
            nodes = nxt
            local.append(tok)
        else:
            flat = [(f, n, j) for f, n in nodes if isinstance(n, list) for j in range(len(n))]
            if not -len(flat) <= tok < len(flat):
                raise IndexError(f"index [{tok}] out of range (merged length {len(flat)})")
            f, n, j = flat[tok]
            nodes = [(f, n[j])]
            local.append(j)
    return Located([f for f, _ in nodes], local, True)


_FLAKE_URL = re.compile(r"(github|gitlab|sourcehut):([^/?]+)/([^/?]+)/([^?]+)(.*)")


def base_url(url: str) -> str:
    """Port of core's _parseFlakeUrl baseUrl: the url with a forge ref stripped."""
    m = _FLAKE_URL.fullmatch(url)
    return f"{m[1]}:{m[2]}/{m[3]}{m[5]}" if m else url


def repo_entries(root: Path, url: str) -> list[tuple[Path, int]]:
    """(file, local index) of every [[icedos.repositories]] entry for ``url``'s repo, in load order."""
    want = base_url(url)
    out = []
    for f in writable_files(root):
        for j, r in enumerate(_read(f).get("icedos", {}).get("repositories", []) or []):
            if base_url(r.get("url", "")) == want:
                out.append((f, j))
    return out


def find_repo_file(root: Path, url: str) -> Path | None:
    """The first config file with a ``[[icedos.repositories]]`` entry for ``url``'s repo."""
    entries = repo_entries(root, url)
    return entries[0][0] if entries else None
