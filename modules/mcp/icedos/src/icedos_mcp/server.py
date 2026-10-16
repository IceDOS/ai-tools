"""IceDOS MCP Server — exposes the IceDOS ecosystem to AI agents."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import functools
import inspect
import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import tomlkit
from mcp.server.fastmcp import Context, FastMCP

from . import build_hosts as remote
from . import config_files
from . import sandbox as sandboxes
from .cli import (
    _ANSI_RE,
    _NOM_FRAME_RE,
    export_search_index,
)
from .cli import (
    doctor as cli_doctor,
)
from .cli import (
    gc as cli_gc,
)
from .cli import (
    genflake_only as cli_genflake_only,
)
from .cli import (
    config_history as cli_config_history,
)
from .cli import (
    get_diff as cli_get_diff,
)
from .cli import (
    list_packages as cli_list_packages,
)
from .cli import (
    nix_eval_expr as cli_nix_eval_expr,
)
from .cli import (
    nixf as cli_nixf,
)
from .cli import (
    pkgs_build as cli_pkgs_build,
)
from .cli import (
    rebuild as cli_rebuild,
)
from .cli import (
    rollback as cli_rollback,
)
from .cli import (
    run_package as cli_run_package,
)
from .cli import (
    summarize_build as cli_summarize_build,
)
from .cli import (
    summarize_log_tail as cli_summarize_log_tail,
)
from .config import (
    add_repository as config_add_repository,
)
from .config import (
    delete_value,
    find_config_root,
    get_config_toml_string,
    get_value,
    live_config_root,
    rel,
    root_override,
    set_value,
    use_config_root,
)
from .config import (
    edit_user as config_edit_user,
)
from .config import (
    list_repositories as config_list_repositories,
)
from .config import (
    set_module_enabled as config_set_module_enabled,
)
from .modules import (
    explore_github_repo,
)
from .modules import (
    explore_local_repo as _explore_local_repo,
)
from .modules import (
    list_repo_tree as modules_list_repo_tree,
)
from .modules import (
    read_repo_file as modules_read_repo_file,
)
from .prompts import get_prompt
from .system import (
    _state_dir,
    get_current_generation,
    get_generations,
    get_system_info,
)
from .system import (
    diff_generations as sys_diff_generations,
)
from .system import (
    list_flake_inputs as sys_list_flake_inputs,
)

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger("icedos-mcp")

mcp = FastMCP("IceDOS MCP Server")


# --- Helpers ---

async def _ensure_search_index() -> None:
    """Regenerate the options/modules search index if stale or missing."""
    try:
        root = find_config_root()
        cache = root / ".state" / ".cache"
        docs = [cache / "options-doc.json", cache / "modules-doc.json"]
        stale = any(not d.exists() for d in docs)
        if not stale:
            oldest = min(d.stat().st_mtime for d in docs)
            # Invalidate index when any source is newer: config files, flake.lock, or generated flake.nix.
            sources = config_files.all_config_tomls(root) + [
                root / "flake.lock",
                root / ".state" / "flake.nix",
            ]
            for src in sources:
                if src.exists() and src.stat().st_mtime > oldest:
                    stale = True
                    break
        if stale:
            logger.info("Regenerating search index...")
            await export_search_index()
    except Exception as e:
        logger.warning("Could not ensure search index: %s", e)


def _load_index(filename: str) -> list[dict[str, Any]]:
    try:
        path = find_config_root() / ".state" / ".cache" / filename
        return json.loads(path.read_text()) if path.exists() else []
    except Exception:
        return []


def _toml_snippet(opt: dict[str, Any]) -> str:
    name, value = opt.get("name", ""), opt.get("value")
    parts = name.split(".")
    section, key = (".".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else ("", name)
    if isinstance(value, str):
        val = f'"{value}"'
    elif isinstance(value, bool):
        val = "true" if value else "false"
    elif value is None:
        val = '""'
    elif isinstance(value, list):
        val = "[ " + ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in value) + " ]"
    else:
        val = str(value)
    return f"[{section}]\n{key} = {val}" if section else f"{key} = {val}"


def _score(query: str, name: str, description: str) -> int:
    """Relevance score for a query against an item's name/description.

    Higher is better; 0 means no match (filtered out). Exact/prefix/word-boundary
    name matches rank above mid-name substring, which ranks above description-only.
    """
    q, n, d = query.lower(), name.lower(), (description or "").lower()
    if not q:
        return 1
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        # earlier match position scores higher; word-boundary gets a bump
        pos = n.index(q)
        bonus = 10 if (pos == 0 or not n[pos - 1].isalnum()) else 0
        return 60 + bonus - min(pos, 20)
    if q in d:
        return 20
    return 0


def _ranked(items: list[dict[str, Any]], query: str, key_name: str,
            limit: int, offset: int) -> dict[str, Any]:
    """Score, sort, and paginate search results with a total count."""
    if query:
        scored = [(s, it) for it in items if (s := _score(query, it.get(key_name, ""), it.get("description", "")))]
        scored.sort(key=lambda si: (-si[0], si[1].get(key_name, "")))
        ordered = [it for _, it in scored]
    else:
        ordered = sorted(items, key=lambda it: it.get(key_name, ""))
    total = len(ordered)
    window = ordered[offset:offset + limit]
    return {
        "total": total,
        "count": len(window),
        "offset": offset,
        "truncated": offset + len(window) < total,
        "results": window,
    }


def _err(kind: str, message: str) -> str:
    """Structured error payload agents can branch on."""
    return json.dumps({"ok": False, "error": {"kind": kind, "message": message}}, indent=2)


_SANDBOX_DOC = """
    sandbox: Name of a sandbox from sandbox_create. Empty uses this server's
             default sandbox, created on first use. Only a server started with
             --live (or ICEDOS_MCP_LIVE=1) runs an empty value on the live config.
"""

# Only the operator can enable live mode, through argv or env. No tool argument can.
LIVE_ENV = "ICEDOS_MCP_LIVE"
_live = os.environ.get(LIVE_ENV, "").strip().lower() in {"1", "true", "yes"}
_default_name: str | None = None


def _default_sandbox() -> str | None:
    """Sandbox for a call without `sandbox=`, created lazily; None in live mode."""
    global _default_name
    if _live:
        return None
    if _default_name:
        try:
            _ = sandboxes.resolve(_default_name)
            return _default_name
        except FileNotFoundError:
            # Fail once rather than silently swap in a fresh copy that lacks the earlier edits.
            gone, _default_name = _default_name, None
            raise FileNotFoundError(
                f"default sandbox '{gone}' was deleted outside this server and its changes are gone; "
                "the next call starts a fresh one"
            ) from None
    _default_name = sandboxes.create()["name"]
    logger.info("default sandbox: %s", _default_name)
    return _default_name


def _live_only(action: str) -> str | None:
    if _live:
        return None
    return _err(
        "live_only",
        f"{action} changes the running system, and this server runs in sandbox mode. "
        f"Ask the user to start it with --live or {LIVE_ENV}=1.",
    )


def _sandboxable(fn):
    """Add a `sandbox` argument that runs the tool against a sandbox copy.

    FastMCP builds the tool schema from the signature, so the new parameter goes
    into `__signature__` with its annotations already evaluated.
    """
    sig = inspect.signature(fn, eval_str=True)
    param = inspect.Parameter("sandbox", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)

    @functools.wraps(fn)
    async def wrapper(*args: Any, sandbox: str = "", **kwargs: Any) -> Any:
        try:
            name = sandbox or _default_sandbox()
            root = sandboxes.resolve(name) if name else None
        except Exception as e:
            return _err("sandbox", str(e))
        if root is None:
            return await fn(*args, **kwargs)
        with use_config_root(root):
            return await fn(*args, **kwargs)

    wrapper.__signature__ = sig.replace(parameters=[*sig.parameters.values(), param])  # type: ignore[attr-defined]
    wrapper.__annotations__ = {**fn.__annotations__, "sandbox": str}
    wrapper.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + _SANDBOX_DOC
    return wrapper


_root_locks: dict[Path, asyncio.Lock] = {}


def _snapshot(root: Path) -> dict[Path, str]:
    return {p: p.read_text() for p in config_files.all_config_tomls(root)}


def _restore(root: Path, snap: dict[Path, str]) -> None:
    for p, text in snap.items():
        if not p.is_file() or p.read_text() != text:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    try:
        for p in config_files.all_config_tomls(root):
            if p not in snap:
                p.unlink()
    except Exception as e:
        logger.warning("could not remove files created by a reverted change: %s", e)


async def _mutate(change, validate: bool) -> str:
    """Run a config write under the root's lock, then check it and revert every file on failure.

    `change` returns a message. The merged config is always re-read, which
    catches TOML and duplicate-key errors at once. `validate` adds an eval of
    the generated flake (`--genflake-only`), which catches option type errors.
    """
    root = find_config_root()
    lock = _root_locks.setdefault(root.resolve(), asyncio.Lock())
    async with lock:
        snap = _snapshot(root)
        try:
            msg = change()
            config_files.merged_config(root)
        except Exception as e:
            _restore(root, snap)
            return _err("write_error", str(e))
        if not validate:
            return f"{msg} (not validated)"
        try:
            rc, _, stderr = await cli_genflake_only()
            err = None if rc == 0 else (stderr.strip() or "unknown eval error")
        except Exception as e:
            err = f"validation could not run: {e}"
        if err:
            _restore(root, snap)
            return _err("validation", f"change reverted: {err}")
        return f"{msg} (validated)"


def _files(paths: list[Path]) -> str:
    return ", ".join(rel(p) for p in paths)


# --- CONFIG TOOLS ---

@mcp.tool()
@_sandboxable
async def get_config(section: str = "", file: str = "") -> str:
    """Read the IceDOS config (merged across config.toml + configs/*.toml).

    Args:
        section: Optional dotted path to a specific section (e.g. 'icedos.system.arch').
        file: Optional config-root-relative file to read in isolation
              (e.g. 'configs/opencode.toml'); default reads the merged config.
    """
    try:
        f = file or None
        if section:
            value = get_value(section, file=f)
            return json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
        return get_config_toml_string(file=f)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
@_sandboxable
async def get_config_value(path: str, resolved: bool = False, file: str = "") -> str:
    """Get a value by dotted path — either declared in config, or the resolved option.

    Array indexing is supported: 'icedos.system.packages[0]',
    'icedos.repositories[0].modules[1]' (negative indices allowed).

    Args:
        path: Dotted path (e.g. 'icedos.system.arch'), with optional '[N]' accessors.
        resolved: If True, return the resolved option value/type from the options
                  index (i.e. the effective default) instead of the declared TOML.
                  Use this for options not written in config.
        file: Optional config-root-relative file to read in isolation; default
              looks the path up in the merged config (values in any
              configs/*.toml are found).
    """
    if resolved:
        await _ensure_search_index()
        for opt in _load_index("options-doc.json"):
            if opt.get("name") == path:
                return json.dumps(
                    {"path": path, "value": opt.get("value"), "type": opt.get("type"), "source": "resolved"},
                    indent=2,
                )
        return _err("not_found", f"option '{path}' not in options index")
    try:
        return json.dumps({"path": path, "value": get_value(path, file=file or None), "source": "declared"}, indent=2)
    except KeyError:
        return _err("not_found", f"'{path}' not declared in config (try resolved=true)")
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
@_sandboxable
async def set_config_value(path: str, value: str, file: str = "", validate: bool = True) -> str:
    """Set a value in the config by dotted path.

    Without `file`, the path reads the merged config: the write lands in the
    file that declares the key (a new key goes next to its nearest existing
    parent, else config.toml), and `[N]` counts items across every file that
    contributes to the array. Accessors:
      - 'icedos.system.packages[2]'  — replace merged element 2 (negatives ok)
      - 'icedos.system.packages[]'   — append a new element
      - 'icedos.repositories[0].modules[1]' — nested list / array-of-tables
      - 'environment.etc."foo.conf".text' — quote keys that hold dots

    Args:
        path: Dotted path (e.g. 'icedos.system.arch'), with optional '[N]'/'[]' accessors.
        value: New value as JSON (string, number, boolean, array, or object).
        file: Config-root-relative target file (e.g. 'configs/gaming.toml', or a
              hidden 'configs/.local.toml' for secrets). Indices are then local
              to that file. Default = auto.
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value

    def change() -> str:
        target = set_value(path, parsed, file=file or None)
        return f"Set {path} = {json.dumps(parsed)} in {rel(target)}{_disabled_note([target])}"

    return await _mutate(change, validate)


def _disabled_note(paths: list[Path]) -> str:
    """Warn when a write lands in a file core does not load (`enable = false`)."""
    loaded = set(config_files.list_config_files(find_config_root()))
    off = [p for p in paths if p.resolve() not in {q.resolve() for q in loaded} and p.exists()]
    return f"; warning: {_files(off)} has enable = false, so core ignores it" if off else ""


@mcp.tool()
@_sandboxable
async def delete_config_value(path: str, file: str = "", validate: bool = True) -> str:
    """Delete a value from the config by dotted path.

    Without `file`, a key is removed from every file that declares it, `[N]` is
    a merged index, and 'path|value' removes the first matching item searching
    the files in load order:
      - 'icedos.system.packages'            — delete the key
      - 'icedos.system.packages[2]'         — delete merged element 2 (negatives ok)
      - 'icedos.system.packages|signal-desktop' — delete the item matching a value

    Args:
        path: Dotted path (optionally with '[N]'), or 'path|value' for value-match removal.
        file: Optional config-root-relative file to delete from; indices are
              then local to it. Default = the declaring files.
        validate: If True, eval-validate (genflake) after deleting; revert on failure.
    """

    def change() -> str:
        return f"Deleted '{path}' from {_files(delete_value(path, file=file or None))}"

    return await _mutate(change, validate)


@mcp.tool()
@_sandboxable
async def list_repositories() -> str:
    """List configured repositories, one row per repo folded over all its entries like core.

    Each row has the url, the files with an entry for it, overrideUrl, modules,
    fetch flags, patches and input-patches. `sandbox_override` shows an override
    set through sandbox_override_repo.
    """
    try:
        return json.dumps(config_list_repositories(), indent=2)
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
@_sandboxable
async def add_repository(
    url: str,
    modules: list[str] | None = None,
    fetch_optional_deps: bool = False,
    fetch_deps: bool = True,
    override_url: str = "",
    patches: list[str] | None = None,
    input_patches: list[dict[str, Any]] | None = None,
    file: str = "",
    validate: bool = False,
) -> str:
    """Add a new [[icedos.repositories]] entry.

    Appends to the config file that already holds repositories (keeping them
    together), else config.toml, or the file you pass. Fails if any config file
    already lists the repo (refs ignored, like core).

    Args:
        url: Repository URL (e.g. 'github:icedos/apps').
        modules: Module names to enable (e.g. ['steam', 'btop']).
        fetch_optional_deps: Also pull optional dependencies.
        fetch_deps: Pull required dependencies (default True; set False to pull none).
        override_url: Local/alternate source written to the config. For test
                      builds use sandbox_override_repo instead.
        patches: Paths to repo-wide patch files (config-root-relative).
        input_patches: Patches for a module's flake input, as
                       [{"module": "...", "input": "...", "patches": ["..."]}].
        file: Optional config-root-relative target file (e.g. 'configs/repositories.toml').
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """

    def change() -> str:
        target = config_add_repository(
            url=url,
            modules=modules or [],
            fetch_optional_deps=fetch_optional_deps,
            fetch_deps=fetch_deps,
            override_url=override_url,
            patches=patches or [],
            input_patches=input_patches or [],
            file=file or None,
        )
        return f"Added repository {url} to {rel(target)}{_disabled_note([target])}"

    return await _mutate(change, validate)


def _module_entries(name: str) -> list[dict[str, Any]]:
    return [m for m in _load_index("modules-doc.json") if m.get("name") == name]


def _module_repo(name: str) -> str:
    """The configured repo that owns a module, via the modules index."""
    repos = sorted({m.get("repo", "") for m in _module_entries(name)})
    if not repos:
        raise LookupError(f"module '{name}' is not in the modules index; pass repo=")
    if len(repos) > 1:
        raise LookupError(f"module '{name}' exists in several repos ({', '.join(repos)}); pass repo=")
    return repos[0]


@mcp.tool()
@_sandboxable
async def enable_module(module: str, repo: str = "", file: str = "", validate: bool = False) -> str:
    """Enable a module by adding it to its repository's `modules` list.

    Entries match by repo url with any ref ignored. The module goes into the
    first entry for the repo, unless any entry already lists it.

    Args:
        module: Module name (e.g. 'steam').
        repo: Owning repo URL. If omitted, resolved from the module index.
        file: Optional config-root-relative file holding the repo; default = the
              first file that declares it.
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    await _ensure_search_index()
    try:
        repo_url = repo or _module_repo(module)
    except LookupError as e:
        return _err("not_found", str(e))

    def change() -> str:
        written = config_set_module_enabled(module, repo_url, enabled=True, file=file or None)
        return f"Enabled '{module}' in {repo_url} ({_files(written)}){_disabled_note(written)}"

    return await _mutate(change, validate)


@mcp.tool()
@_sandboxable
async def disable_module(module: str, repo: str = "", file: str = "", validate: bool = False) -> str:
    """Disable a module by removing it from every `modules` list of its repository.

    A module pulled in only as a dependency is not in any list; disable the
    modules that depend on it instead (see module_graph).

    Args:
        module: Module name (e.g. 'steam').
        repo: Owning repo URL. If omitted, resolved from the module index.
        file: Optional config-root-relative file; default = every file that lists it.
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    await _ensure_search_index()
    try:
        repo_url = repo or _module_repo(module)
    except LookupError as e:
        return _err("not_found", str(e))

    def change() -> str:
        try:
            written = config_set_module_enabled(module, repo_url, enabled=False, file=file or None)
        except ValueError:
            dep = [m for m in _module_entries(module) if m.get("enabled") and not m.get("explicit")]
            if dep:
                raise ValueError(
                    f"'{module}' is only enabled as a dependency; disable the modules that need it (module_graph)"
                ) from None
            raise
        return f"Disabled '{module}' in {repo_url} ({_files(written)})"

    return await _mutate(change, validate)


@mcp.tool()
@_sandboxable
async def edit_user(username: str, settings: str = "{}", file: str = "", validate: bool = False) -> str:
    """Add or update keys of a user under [icedos.users.<username>].

    Each key lands in the file that declares it, else the file declaring the
    user, else config.toml. Keys not in `settings` stay as they are.

    Args:
        username: The username.
        settings: JSON object with keys: initialPassword, sudo, extraGroups,
                  packages, trusted, isNormalUser, isSystemUser, home, description.
        file: Optional config-root-relative target file for every key.
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    try:
        parsed = json.loads(settings)
    except json.JSONDecodeError:
        return _err("write_error", "settings must be a valid JSON object")

    def change() -> str:
        written = config_edit_user(username, parsed, file=file or None)
        return f"Updated user '{username}' in {_files(written)}{_disabled_note(written)}"

    return await _mutate(change, validate)


# --- QUERY & DISCOVERY ---

@mcp.tool()
@_sandboxable
async def search_options(query: str = "", category: str = "", limit: int = 50, offset: int = 0) -> str:
    """Search IceDOS options by name/description, ranked, with pagination.

    Returns {total, count, offset, truncated, results}. Results are relevance-ranked
    (exact > prefix > substring > description). Raise `offset` to page through `total`.

    Args:
        query: Search term.
        category: Substring filter on the option name (e.g. 'hardware', 'system').
        limit: Max results in this page (default 50).
        offset: Skip this many ranked results (for pagination).
    """
    await _ensure_search_index()
    opts = _load_index("options-doc.json")
    if not opts:
        return _err("no_index", "no options index; run 'icedos rebuild' first")
    if category:
        c = category.lower()
        opts = [o for o in opts if c in o.get("name", "").lower()]
    return json.dumps(_ranked(opts, query, "name", limit, offset), indent=2)


@mcp.tool()
@_sandboxable
async def get_option(name: str) -> str:
    """Get full details for a specific IceDOS option with a paste-ready TOML snippet.

    Args:
        name: Option name (e.g. 'icedos.system.arch').
    """
    await _ensure_search_index()
    for opt in _load_index("options-doc.json"):
        if opt.get("name") == name:
            return json.dumps({**opt, "toml_snippet": _toml_snippet(opt)}, indent=2)
    return f"Option '{name}' not found."


@mcp.tool()
@_sandboxable
async def search_modules(query: str = "", repo: str = "", status: str = "all",
                         limit: int = 50, offset: int = 0) -> str:
    """Search IceDOS modules, ranked, with pagination.

    Returns {total, count, offset, truncated, results}, relevance-ranked.

    Args:
        query: Search term.
        repo: Filter by repo URL (e.g. 'github:icedos/apps').
        status: 'enabled', 'available', or 'all' (default).
        limit: Max results in this page (default 50).
        offset: Skip this many ranked results (for pagination).
    """
    await _ensure_search_index()
    mods = _load_index("modules-doc.json")
    if not mods:
        return _err("no_index", "no modules index; run 'icedos rebuild' first")
    if repo:
        mods = [m for m in mods if m.get("repo") == repo]
    if status == "enabled":
        mods = [m for m in mods if m.get("enabled")]
    elif status == "available":
        mods = [m for m in mods if not m.get("enabled")]
    return json.dumps(_ranked(mods, query, "name", limit, offset), indent=2)


@mcp.tool()
@_sandboxable
async def module_graph(name: str) -> str:
    """Dependency graph for a module — forward (deps) and reverse (dependents).

    Forward = what `name` pulls in (recursively, deps + optionalDeps). Reverse =
    which other modules depend on `name`. Sourced from the module index; no eval.

    Args:
        name: Module name (e.g. 'steam').
    """
    await _ensure_search_index()
    mods = {m["name"]: m for m in _load_index("modules-doc.json") if m.get("name")}
    if name not in mods:
        return _err("not_found", f"module '{name}' not in index")

    def deps_of(mod: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for group in mod.get("dependencies", []) + mod.get("optionalDependencies", []):
            out.extend(group.get("modules", []) if isinstance(group, dict) else [])
        return out

    # forward, recursive
    forward, seen, stack = [], set(), list(deps_of(mods[name]))
    while stack:
        d = stack.pop(0)
        if d in seen:
            continue
        seen.add(d)
        node = mods.get(d, {})
        forward.append({"name": d, "enabled": node.get("enabled", False), "present": d in mods})
        stack.extend(x for x in deps_of(node) if x not in seen)

    reverse = sorted(other for other, m in mods.items() if name in deps_of(m))

    node = mods[name]
    return json.dumps(
        {
            "module": name,
            "repo": node.get("repo"),
            "enabled": node.get("enabled", False),
            "explicit": node.get("explicit", False),
            "depends_on": forward,
            "required_by": reverse,
        },
        indent=2,
    )


@mcp.tool()
@_sandboxable
async def get_module(name: str) -> str:
    """Get full details for a specific IceDOS module.

    Args:
        name: Module name (e.g. 'steam', 'btop').
    """
    await _ensure_search_index()
    for mod in _load_index("modules-doc.json"):
        if mod.get("name") == name:
            return json.dumps(mod, indent=2)
    return f"Module '{name}' not found."


@mcp.tool()
@_sandboxable
async def list_modules(repo: str = "", status: str = "all") -> str:
    """List all IceDOS modules with status markers (● explicit, ◐ dependency, ○ available).

    Args:
        repo: Filter by repo URL.
        status: 'enabled', 'available', or 'all' (default).
    """
    await _ensure_search_index()
    mods = _load_index("modules-doc.json")
    if not mods:
        return "No modules index found."
    if repo:
        mods = [m for m in mods if m.get("repo") == repo]
    if status == "enabled":
        mods = [m for m in mods if m.get("enabled")]
    elif status == "available":
        mods = [m for m in mods if not m.get("enabled")]
    lines = []
    for m in mods:
        marker = "●" if m.get("explicit") else ("◐" if m.get("enabled") else "○")
        lines.append(f"{marker} {m['name']}  ({m.get('repo', '?')})")
        if m.get("description"):
            lines.append(f"  {m['description']}")
    return "\n".join(lines) if lines else "No modules found."


# --- SANDBOXES ---

# icedos rebuild flags the tool accepts. Value flags take the next word.
_REBUILD_FLAGS = {
    "--dry", "-n", "--dry-run", "--logs", "--update", "--update-core", "--update-core-only",
    "--update-repos", "--update-repos-only", "--update-repo-inputs-only", "--lock-only",
    "--genflake-only", "--build-vm",
}
_REBUILD_VALUE_FLAGS = {"--update-state-inputs", "--update-repos-select", "--update-except", "--github-token-path"}
# Live only: they activate or deploy a system, run hooks, or forward raw args to nh/nix.
# nh copies a --builder result back, which fails unless the remote user is trusted.
_REBUILD_LIVE_FLAGS = {"--boot", "--update-hooks"}
_REBUILD_LIVE_VALUE_FLAGS = {"--target", "--builder"}
_REBUILD_LIVE_REST_FLAGS = {"--nh-args", "--build-args"}
_REBUILD_REJECT = {
    "--dir": "the server picks the config dir; use sandbox= instead",
    "--run-vm": "it starts an interactive VM; use --build-vm and run the script yourself",
    "--ask": "it waits for a confirmation the server cannot give",
    "--github-token": "a literal token would land in logs; use --github-token-path",
    "--build": "the server adds it",
}


def _check_rebuild_flags(extra: list[str], in_sandbox: bool) -> str | None:
    i = 0
    while i < len(extra):
        f = extra[i]
        if f in _REBUILD_REJECT:
            return f"{f} is not supported: {_REBUILD_REJECT[f]}"
        live_only = f in _REBUILD_LIVE_FLAGS | _REBUILD_LIVE_VALUE_FLAGS | _REBUILD_LIVE_REST_FLAGS
        if live_only and in_sandbox:
            hint = "; test-build on a remote host with nix_build instead" if f == "--builder" else ""
            return f"{f} is not allowed in a sandbox rebuild{hint}"
        if f in _REBUILD_LIVE_REST_FLAGS:
            return None
        if f in _REBUILD_VALUE_FLAGS | _REBUILD_LIVE_VALUE_FLAGS:
            if i + 1 >= len(extra) or extra[i + 1].startswith("--"):
                return f"{f} needs a value"
            i += 2
            continue
        if f not in _REBUILD_FLAGS and not live_only:
            return f"unknown rebuild flag {f}"
        i += 1
    return None


@mcp.tool()
async def sandbox_create(name: str = "") -> str:
    """Copy the live config into a new throwaway sandbox for edits and test builds.

    Calls without `sandbox=` already use this server's default sandbox, so you
    need this only for a separate copy, e.g. one per subagent sharing this
    server. Pass the returned name as `sandbox=` to the config, module, query,
    state, nix_eval, get_diff and rebuild tools. A sandbox `rebuild` runs
    `icedos rebuild --dir <sandbox> --build`, which never activates and never
    writes the live config or its `.state`. Delete it with sandbox_delete.

    Args:
        name: Optional name (letters, digits, '.', '_', '-'); default is random.
    """
    try:
        return json.dumps(sandboxes.create(name), indent=2)
    except Exception as e:
        return _err("sandbox", str(e))


@mcp.tool()
async def sandbox_list() -> str:
    """Show the server mode, this server's default sandbox and every existing sandbox.

    `mode` is "sandbox" (the default: calls without `sandbox=` use `default`) or
    "live" (calls without `sandbox=` use the real config). `default` is null
    until the first call that needs it.
    """
    try:
        return json.dumps(
            {
                "mode": "live" if _live else "sandbox",
                "default": _default_name,
                "sandboxes": sandboxes.list_all(),
            },
            indent=2,
        )
    except Exception as e:
        return _err("sandbox", str(e))


@mcp.tool()
async def sandbox_diff(name: str) -> str:
    """Unified diff of the live config against a sandbox (a/ = live, b/ = sandbox).

    Covers every file in the config root except `.git` and `.state`, so the
    output is a patch the user can review and apply to the live config.

    Args:
        name: Sandbox name.
    """
    try:
        return _with_drift(name, sandboxes.diff(name) or "No differences.")
    except Exception as e:
        return _err("sandbox", str(e))


def _with_drift(name: str, text: str) -> str:
    drift = sandboxes.live_drift(name)
    if not drift:
        return text
    return (
        f"Note: the live config changed since this sandbox was copied ({', '.join(drift)}). "
        "The diff only holds sandbox edits; check it still applies.\n\n" + text
    )


@mcp.tool()
@_sandboxable
async def sandbox_inputs_diff() -> str:
    """Flake input revs that differ between a sandbox and the live config.

    Compares the config flake.lock and the generated `.state/flake.lock` of the
    sandbox against the live ones, e.g. after a sandbox `rebuild --update`.
    """
    root = root_override()
    if root is None:
        return _err("sandbox", "needs a sandbox; pass sandbox= or use sandbox mode")
    live = live_config_root()
    out: dict[str, Any] = {}
    for lock in ("flake.lock", ".state/flake.lock"):
        a, b = _lock_revs(live / lock), _lock_revs(root / lock)
        changed = {
            k: {"live": a.get(k), "sandbox": b.get(k)} for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)
        }
        if changed:
            out[lock] = changed
    return json.dumps(out, indent=2) if out else "No input differences."


def _lock_revs(path: Path) -> dict[str, str]:
    try:
        nodes = json.loads(path.read_text()).get("nodes", {})
    except (OSError, ValueError):
        return {}
    revs = {}
    for name, node in nodes.items():
        locked = node.get("locked", {})
        rev = locked.get("rev") or locked.get("narHash") or locked.get("path")
        if rev:
            revs[name] = rev
    return revs


@mcp.tool()
async def sandbox_delete(name: str) -> str:
    """Delete a sandbox directory. Store paths its builds made go away with `gc`.

    Args:
        name: Sandbox name.
    """
    global _default_name
    try:
        sandboxes.delete(name)
        if name == _default_name:
            _default_name = None
        return f"Deleted sandbox '{name}'."
    except Exception as e:
        return _err("sandbox", str(e))


@mcp.tool()
@_sandboxable
async def sandbox_override_repo(repo: str, path: str = "") -> str:
    """Build an icedos repo from a local checkout inside a sandbox, without editing config files.

    Use it to test changes to a module repo (core excluded) in a sandbox build.
    The server keeps the override in its own file inside the sandbox, which
    sandbox_diff leaves out, so the user's config never gains an overrideUrl.
    Only an `overrideUrl` set in the sandbox's config.toml takes precedence.

    Args:
        repo: Full flake url (e.g. 'github:icedos/ai-tools') or the repo name
              ('ai-tools') of a configured repository.
        path: Local checkout holding the repo's flake.nix ('path:' prefix
              optional). Empty clears the override.
    """
    sandbox_root = root_override()
    if sandbox_root is None:
        return _err("sandbox", "repo overrides only apply inside a sandbox; pass sandbox= or use sandbox mode")
    try:
        return json.dumps(sandboxes.set_override(sandbox_root, repo, path), indent=2)
    except Exception as e:
        return _err("sandbox", str(e))


# --- SYSTEM OPERATIONS ---

@mcp.tool()
@_sandboxable
async def rebuild(ctx: Context, flags: str = "", logs: bool = False) -> str:
    """Run `icedos rebuild --build`: evaluate and build the closure without switching to it.

    Output is condensed to the build summary by default (the raw nom stream is huge);
    pass logs=true for the full log with --show-trace. Progress is streamed as MCP
    progress notifications so long builds keep the client's tool call alive. On
    success the result names the built store path.

    Args:
        flags: Extra `icedos rebuild` flags:
               --dry/-n/--dry-run, --genflake-only (eval and lock only),
               --update, --update-core, --update-core-only, --update-repos,
               --update-repos-only, --update-repo-inputs-only,
               --update-repos-select "<urls>", --update-state-inputs "<inputs>",
               --update-except "<inputs>", --lock-only (with an --update flag),
               --build-vm, --github-token-path <file>.
               Live mode only: --boot, --builder <host>, --target <host>, --update-hooks,
               --nh-args ..., --build-args ...
               In a sandbox, --update* flags change only the sandbox's locks.
        logs: Return the full raw log instead of the condensed summary.
    """
    try:
        extra = shlex.split(flags) if flags else []
    except ValueError as e:
        return _err("bad_flags", f"cannot parse flags: {e}")
    if bad := _check_rebuild_flags(extra, root_override() is not None):
        return _err("bad_flags", bad)
    if logs and "--logs" not in extra:
        extra.append("--logs")

    try:
        rc, stdout, stderr = await cli_rebuild(*extra, timeout=3600, on_line=_progress_reporter(ctx))
    except TimeoutError as e:
        partial = getattr(e, "partial_output", "")
        detail = str(e)
        if partial:
            # Condense partial log the same way as a successful build (ANSI-stripped, redraw-free, size-capped).
            detail += f"\n--- last output ---\n{cli_summarize_log_tail(partial)}"
        return _err("timeout", detail)
    out = (stdout + (f"\n--- stderr ---\n{stderr}" if stderr else "")) if logs else cli_summarize_build(stdout, stderr)
    if rc != 0:
        return f"Build failed (exit {rc}):\n{out}"
    built = _built_path(stdout)
    return "Build succeeded!" + (f"\nresult: {built}" if built else "") + f"\n{out}"


_BUILD_DIR_RE = re.compile(r"building from path (\S+?)\.{3}")


def _built_path(log: str) -> str | None:
    """Store path of the closure, read from the build dir's result link."""
    for m in reversed(_BUILD_DIR_RE.findall(_ANSI_RE.sub("", log))):
        try:
            return os.readlink(Path(m) / "result")
        except OSError:
            continue
    return None


@mcp.tool()
@_sandboxable
async def get_diff() -> str:
    """Show pending config changes: in a sandbox, its edits as a unified diff
    (same as sandbox_diff); live, the config against the one that built the running system."""
    sandbox_root = root_override()
    if sandbox_root is not None:
        try:
            name = sandbox_root.name
            return _with_drift(name, sandboxes.diff(name) or "No differences from the live config.")
        except Exception as e:
            return _err("sandbox", str(e))
    _rc, stdout, stderr = await cli_get_diff()
    if stdout:
        return stdout
    if "no config snapshots" in (stderr or ""):
        return "No previous build found."
    return "No pending changes."


@mcp.tool()
async def rollback(generation: int | None = None, dry: bool = False) -> str:
    """Roll back to a previous system generation.

    Needs a server in live mode, except with `dry`.

    Args:
        generation: Target generation number (omit for previous).
        dry: Show the plan without changing anything.
    """
    if not dry and (blocked := _live_only("rollback")):
        return blocked
    rc, stdout, stderr = await cli_rollback(generation=generation, dry=dry)
    out = stdout + (f"\n{stderr}" if stderr else "")
    return out if rc == 0 else f"Rollback failed (exit {rc}):\n{out}"


@mcp.tool()
async def gc(days: int | None = None, generations: int | None = None, dry: bool = False, timeout: float = 1800) -> str:
    """Free up disk space by cleaning the Nix store, home-manager profiles, temp build dirs,
    and .state/.cache snapshots.

    `days`/`generations` are shared retention overrides for the Nix store AND the cache
    snapshots (days = keep newer than N days; generations = keep N newest; 0 = aggressive).
    Generations are system rollback points — a small `generations` value irreversibly drops
    them, so prefer `dry` first. This command needs root; without a passwordless sudo rule
    it will fail with a sudo error rather than prompt. Needs a server in live mode, except
    with `dry`.

    Args:
        days: Retention in days for the store and cache snapshots (None = use config default).
        generations: Number of newest store/system generations and cache snapshots to keep
                     (None = use config default; 0 = aggressive).
        dry: Preview what would be deleted without actually cleaning.
        timeout: Max seconds to wait before killing the process (default 1800).
    """
    if not dry and (blocked := _live_only("gc")):
        return blocked
    try:
        rc, stdout, stderr = await cli_gc(days=days, generations=generations, dry=dry, timeout=timeout)
    except TimeoutError as e:
        return _err("timeout", str(e))
    out = stdout + (f"\n{stderr}" if stderr else "")
    return out if rc == 0 else f"GC failed (exit {rc}):\n{out}"


@mcp.tool()
async def doctor() -> str:
    """Run IceDOS health checks (substituters, cache, hardware, store, gc, inputs)."""
    rc, stdout, stderr = await cli_doctor()
    out = stdout or stderr
    return out if rc == 0 else f"{out}\n\n(status reported failures — exit {rc})"


# --- PACKAGE MANAGEMENT ---

@mcp.tool()
async def list_packages() -> str:
    """List all installed packages on the system."""
    rc, stdout, stderr = await cli_list_packages()
    if rc != 0:
        return _err("cli_error", stderr.strip() or "pkgs list failed")
    return stdout


@mcp.tool()
async def run_package(attr: str, args: list[str] | None = None, insecure: bool = False, timeout: int = 300) -> str:
    """Run a nixpkgs package's main binary without installing it (`icedos pkgs run`).

    Blocks until it exits or times out, so it is not for long-lived GUI apps.

    Args:
        attr: Package attribute (e.g. 'ripgrep', 'btop').
        args: Arguments for the binary, e.g. ['--version'].
        insecure: Allow packages marked insecure.
        timeout: Max seconds to wait before killing the process (default 300).
    """
    try:
        rc, stdout, stderr = await cli_run_package(attr, args=args, insecure=insecure, timeout=timeout)
    except TimeoutError as e:
        return _err("timeout", str(e))
    out = stdout + (f"\n{stderr}" if stderr else "")
    return out if rc == 0 else f"exit {rc}:\n{out}"


@mcp.tool()
async def pkgs_build(path: str, run: str = "", args: list[str] | None = None, timeout: int = 1800) -> str:
    """Build a package.nix against the system's pkgs (`icedos pkgs build`).

    The package is called with icedosLib.packaging supplied, like a module's
    packages. Returns the store path, or the binary's output with `run`. Uses
    the live system's generated flake, so it needs a prior live rebuild.

    Args:
        path: Path to a package.nix file.
        run: Binary name to launch from the build's bin/ after building.
        args: Arguments for that binary (needs `run`).
        timeout: Max seconds (default 1800).
    """
    if args and not run:
        return _err("bad_args", "args need run=")
    try:
        rc, stdout, stderr = await cli_pkgs_build(str(Path(path).expanduser()), run=run, args=args, timeout=timeout)
    except TimeoutError as e:
        return _err("timeout", str(e))
    out = _ANSI_RE.sub("", stdout + (f"\n{stderr}" if stderr else "")).strip()
    return out if rc == 0 else f"Build failed (exit {rc}):\n{cli_summarize_log_tail(out)}"


# --- REMOTE BUILD HOSTS ---

@mcp.tool()
async def build_hosts(need_gb: float = 0) -> str:
    """Check whether a heavy build should run on this PC or a remote build host.

    Probes this PC (load, CPU and IO pressure) and every host in
    icedos.ai-tools.mcp-server.build.hosts over ssh (cores, load, free space on
    /nix/store). This PC counts as busy when its 1 minute load is above its core
    count or a `some avg10` pressure is above the limit. A host qualifies when its
    load is below half its cores and it has the minimum free space plus need_gb.
    Among qualifying hosts, the one with the fewest builds from any of this user's
    MCP servers wins, then the lowest load per core.

    Args:
        need_gb: Extra free space the build needs on the host, in GB.

    Returns JSON with `pick` ("local" or a host), `why`, and the probe of each machine.
    """
    return json.dumps(await remote.choose(need_gb), indent=2)


def _progress_reporter(ctx: Context):
    """Line callback that sends at most one progress notification per 5s of real output."""
    state = {"last": 0.0, "n": 0}

    async def report(line: str) -> None:
        now = time.monotonic()
        if now - state["last"] < 5.0:
            return
        msg = _ANSI_RE.sub("", line).strip()
        if not msg or _NOM_FRAME_RE.match(msg):
            return
        state["last"] = now
        state["n"] += 1
        try:
            await ctx.report_progress(progress=state["n"], total=None, message=msg[:120])
        except Exception:
            pass

    return report


@mcp.tool()
async def nix_build(
    ctx: Context, installable: str, host: str = "auto", need_gb: float = 0, timeout: int = 3600, logs: bool = False
) -> str:
    """Build a Nix installable on this PC or a remote build host, without a result link.

    A remote build evaluates here and builds with `--eval-store auto --store
    ssh-ng://<host>`. Its outputs stay in the host's store, because the remote
    user is not trusted and cannot sign them for this PC. Inspect them over ssh.
    The server records every path the build added to the host and deletes them
    with remote_cleanup or when it exits. Never use this for IceDOS rebuilds;
    those need the local store.

    Args:
        installable: Flake ref or store path, e.g. 'nixpkgs#hello' or
                     '/abs/path/to/flake#packages.x86_64-linux.foo'. Use absolute
                     paths; the server's working dir is not yours.
        host: "auto" asks build_hosts, "local" builds here, or a configured host.
              Keep small builds and evaluations on "local".
        need_gb: Extra free space the build needs on the host, for "auto".
        timeout: Max seconds (default 3600).
        logs: Return the full log instead of its tail.
    """
    if not installable or installable.startswith("-"):
        return _err("bad_args", "installable must be a flake ref or store path, not a flag")
    hosts = remote.settings()["hosts"]
    picked = None
    if host == "auto":
        picked = await remote.choose(need_gb)
        host = picked["pick"]
    elif host != "local" and host not in hosts:
        return _err("bad_args", f"unknown host {host!r}; configured hosts: {', '.join(hosts) or 'none'}")
    try:
        rc, log, result = await remote.build(installable, host, timeout, on_line=_progress_reporter(ctx))
    except TimeoutError as e:
        partial = getattr(e, "partial_output", "")
        if host != "local":
            _ = remote.record(host, partial)
        return _err("timeout", f"{e}\n--- last output ---\n{cli_summarize_log_tail(partial)}")
    added = remote.record(host, log) if host != "local" else set()
    clean = _ANSI_RE.sub("", log).strip()
    out = clean if logs else cli_summarize_log_tail(clean)
    where = f"on {host}" + (f" ({picked['why']})" if picked else "")
    if rc != 0:
        return f"Build failed {where} (exit {rc}):\n{out}"
    paths = [p for r in result for p in r.get("outputs", {}).values()]
    lines = [f"Build succeeded {where}.", *(f"output: {p}" for p in paths)]
    if host != "local":
        lines.append(f"The outputs live in {host}'s store; inspect them with ssh {host}. "
                     f"{len(added)} new paths there are queued for remote_cleanup.")
    return "\n".join(lines) + f"\n{out}"


@mcp.tool()
async def remote_cleanup(host: str = "") -> str:
    """Delete the paths this server's nix_build runs added to a build host.

    Deletes built outputs, their .drv files and every path copied or substituted
    onto the host, never paths it already had. Paths something else still uses on
    the host stay. The server also runs this when it exits.

    Args:
        host: One host, or empty for every host this server built on.
    """
    report = await asyncio.to_thread(remote.cleanup, host)
    return json.dumps(report or {"note": "no remote builds recorded"}, indent=2)


# --- REPO EXPLORER ---

@mcp.tool()
@_sandboxable
async def explore_repo(url: str) -> str:
    """Explore an IceDOS module repo — METADATA ONLY (checks local override, then GitHub).

    Returns per-module meta.name/description/dependencies (regex-scraped from icedos.nix)
    and config.toml default values. It does NOT return source: for actual file contents
    (scripts.nix, options.nix, packages.nix, ...) use `list_repo_tree` to see what exists
    and `read_repo_file` to read it.

    Args:
        url: Repository URL (e.g. 'github:icedos/apps').
    """
    try:
        modules = await explore_github_repo(url)
        if not modules:
            return f"No modules found in {url}."
        return json.dumps(modules, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def explore_local_repo(path: str) -> str:
    """Explore a local IceDOS module repo checkout — METADATA ONLY.

    Scans modules/*/icedos.nix for meta and reads config.toml defaults. It does NOT
    return source: for actual file contents use `list_repo_tree` + `read_repo_file`.

    Args:
        path: Absolute path to the local repo checkout.
    """
    try:
        modules = _explore_local_repo(path)
        if not modules:
            return f"No modules found in {path}."
        return json.dumps(modules, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
@_sandboxable
async def read_repo_file(repo: str, path: str, offset: int = 0, limit: int = 0) -> str:
    """Read the raw contents of a single file in an IceDOS repo — local OR online.

    Returns ACTUAL FILE SOURCE (e.g. scripts.nix, options.nix, packages.nix), unlike
    explore_repo/explore_local_repo which return only metadata. Use this to read a
    module's real implementation instead of inferring it from meta.

    Args:
        repo: A local path ('/home/ice/Projects/icedos/apps' or 'path:...'), or a
              'github:owner/repo[/ref]' URL. GitHub URLs with a path: overrideUrl in
              config.toml are served from the local checkout automatically.
        path: Repo-relative file path (e.g. 'modules/steam/scripts.nix').
        offset: 0-based first line to return (default 0 = from start).
        limit: Max lines to return (0 = all).
    """
    try:
        text = await modules_read_repo_file(repo, path)
    except FileNotFoundError as e:
        return _err("not_found", str(e))
    except ValueError as e:
        return _err("bad_path", str(e))
    except Exception as e:
        return _err("read_error", str(e))
    if offset or limit:
        lines = text.splitlines()
        text = "\n".join(lines[offset : (offset + limit) if limit else len(lines)])
    return text


@mcp.tool()
@_sandboxable
async def list_repo_tree(repo: str, subdir: str = "") -> str:
    """List every file path in an IceDOS repo — local OR online.

    Use this to discover which files a module ships (scripts.nix, options.nix,
    packages.nix, ...) before fetching one with `read_repo_file`.

    Args:
        repo: Local path / 'path:...' / 'github:owner/repo[/ref]' (override-aware).
        subdir: Optional path prefix to filter (e.g. 'modules/steam').
    """
    try:
        files = await modules_list_repo_tree(repo, subdir)
    except FileNotFoundError as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("read_error", str(e))
    if not files:
        return _err("not_found", f"no files in {repo}" + (f"/{subdir}" if subdir else ""))
    return json.dumps({"repo": repo, "count": len(files), "files": files}, indent=2)


# --- SYSTEM INFO ---

@mcp.tool()
async def get_system_info_tool() -> str:
    """Get hostname, kernel, uptime, current generation, and store disk space."""
    try:
        return json.dumps(await get_system_info(), indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_generations_tool() -> str:
    """List all system generations with dates and the config snapshot that built each."""
    try:
        gens = await get_generations()
        if not gens:
            return "No generations found."
        current = await get_current_generation()
        lines = []
        for g in gens:
            marker = " (current)" if g["generation"] == current else ""
            snap = f"  [{g['config_snapshot']}]" if g.get("config_snapshot") else ""
            lines.append(f"  Generation {g['generation']}: {g['date']}{marker}{snap}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
@_sandboxable
async def list_inputs() -> str:
    """Flake inputs pinned in .state/flake.lock: rev, source and age.

    Live, these built the running system. In a sandbox they are the sandbox's
    pins (copied from live, changed by a sandbox `rebuild --update*`); see
    sandbox_inputs_diff for the difference.
    """
    try:
        return json.dumps(sys_list_flake_inputs(), indent=2)
    except FileNotFoundError:
        return _err("no_lock", "no .state/flake.lock; run 'icedos rebuild' first")
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
async def diff_generations(generation_a: int, generation_b: int) -> str:
    """Diff the config set that built two generations.

    Compares every config file (config.toml + all extra-config dirs) from each
    generation's snapshot, not just config.toml.

    Args:
        generation_a: First generation number.
        generation_b: Second generation number.
    """
    try:
        return sys_diff_generations(generation_a, generation_b)
    except (KeyError, FileNotFoundError) as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("read_error", str(e))


# --- STATE FILES ---

@mcp.tool()
@_sandboxable
async def list_state_files(subdir: str = "") -> str:
    """List files and directories under .state/.

    Shows what generated state files exist: flake.nix, flake.lock,
    .cache/ snapshots, generation records, etc.

    Args:
        subdir: Optional subdirectory path within .state/ (e.g. '.cache/generations').
    """
    try:
        state = _state_dir()
        target = state / subdir if subdir else state
        target_resolved = target.resolve()
        state_resolved = state.resolve()
        if not target_resolved.is_relative_to(state_resolved):
            return _err("bad_path", "path traversal not allowed")
        if not target.is_dir():
            return _err("not_found", f"{subdir or '.state/'} is not a directory or does not exist")
        entries = []
        for entry in sorted(target.iterdir()):
            entry_type = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else None
            rel = entry.relative_to(state)
            entries.append({
                "name": str(rel),
                "type": entry_type,
                "size": size,
            })
        return json.dumps({"count": len(entries), "entries": entries}, indent=2)
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
@_sandboxable
async def read_state_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file from .state/.

    Useful for inspecting the generated flake.nix, flake.lock, build.sh,
    or cached snapshots and indices.

    Args:
        path: File path relative to .state/ (e.g. 'flake.nix', '.cache/options-doc.json').
        offset: 0-based first line to return (default 0 = from start).
        limit: Max lines to return (0 = all).
    """
    try:
        state = _state_dir()
        target = state / path
        target_resolved = target.resolve()
        state_resolved = state.resolve()
        if not target_resolved.is_relative_to(state_resolved):
            return _err("bad_path", "path traversal not allowed")
        if not target.is_file():
            return _err("not_found", f"'{path}' not found in .state/")
        text = target.read_text()
        if offset or limit:
            lines = text.splitlines()
            text = "\n".join(lines[offset : (offset + limit) if limit else len(lines)])
        return text
    except Exception as e:
        return _err("read_error", str(e))


# --- CONFIG FILES, VALIDATION, FORMATTING ---

@mcp.tool()
@_sandboxable
async def list_config_files() -> str:
    """Every config file in load order, with whether core loads it.

    config.toml first, then each *.toml under the extraConfigs dirs sorted by
    path. A file with top-level `enable = false` is listed with loaded=false.
    """
    try:
        root = find_config_root()
        loaded = set(config_files.list_config_files(root))
        rows = [
            {
                "file": p.relative_to(root).as_posix(),
                "loaded": p in loaded,
                "managed_by_server": p.name == config_files.OVERRIDES_FILE,
            }
            for p in config_files.all_config_tomls(root)
        ]
        return json.dumps(rows, indent=2)
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
@_sandboxable
async def set_config_file_enabled(file: str, enabled: bool, validate: bool = False) -> str:
    """Turn a whole extra config file on or off through its top-level `enable` key.

    Core skips a file with `enable = false`. config.toml always loads.

    Args:
        file: Config-root-relative file, e.g. 'configs/gaming.toml'.
        enabled: False writes `enable = false`; True removes the key.
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """

    def change() -> str:
        root = find_config_root()
        target = config_files.resolve_file(root, file)
        if target == (root / "config.toml").resolve():
            raise ValueError("config.toml always loads; it has no enable switch")
        if not target.is_file():
            raise FileNotFoundError(f"'{file}' does not exist")
        doc = tomlkit.loads(target.read_text())
        if enabled:
            doc.pop("enable", None)
        elif "enable" in doc:
            doc["enable"] = False
        else:
            # Top-level keys must come before the first table.
            body = doc.as_string()
            target.write_text(f"enable = false\n\n{body}" if body.strip() else "enable = false\n")
            return f"Disabled {rel(target)}"
        target.write_text(tomlkit.dumps(doc))
        return f"{'Enabled' if enabled else 'Disabled'} {rel(target)}"

    return await _mutate(change, validate)


@mcp.tool()
@_sandboxable
async def validate_config() -> str:
    """Evaluate the config without building, like `icedos configuration validate`.

    Runs the search-index export, which evaluates every option and module, and
    refreshes the index used by search_options and search_modules.
    """
    try:
        root = find_config_root()
        config_files.merged_config(root)
        rc, stdout, stderr = await export_search_index()
    except Exception as e:
        return _err("validation", str(e))
    if rc != 0:
        return _err("validation", cli_summarize_log_tail(stderr or stdout))
    return "Config is valid."


@mcp.tool()
async def config_history(generation: int | None = None) -> str:
    """System generations with their config snapshots, or one generation's config diff.

    Without `generation`, returns `icedos configuration history --json`. With
    it, the diff between that generation's config snapshot and the live config.

    Args:
        generation: Generation number to diff against the live config.
    """
    rc, stdout, stderr = await cli_config_history(generation)
    out = _ANSI_RE.sub("", stdout + (f"\n{stderr}" if stderr else ""))
    return out if rc == 0 else _err("cli_error", out.strip())


@mcp.tool()
@_sandboxable
async def format_nix(path: str) -> str:
    """Format .nix files in place with nixfmt (`icedos nixf`), recursively for a dir.

    A relative path is resolved against the active config root (the sandbox
    copy in sandbox mode). In sandbox mode paths inside the live config are
    refused; format a module checkout or the sandbox instead.

    Args:
        path: File or directory to format.
    """
    root = find_config_root()
    target = (root / Path(path).expanduser()).resolve()
    if not target.exists():
        return _err("not_found", f"'{target}' does not exist")
    if not _live and target.is_relative_to(live_config_root().resolve()):
        return _err("live_only", "formatting the live config needs a server in live mode")
    rc, stdout, stderr = await cli_nixf(str(target))
    out = (stdout + stderr).strip()
    return f"Formatted {target}" + (f"\n{out}" if out else "") if rc == 0 else _err("cli_error", out)


# --- RESOURCES ---

def _on_default_root(fn):
    """Run a resource on the root a tool call without sandbox= would use."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            name = _default_sandbox()
            if name is None:
                return await fn(*args, **kwargs)
            with use_config_root(sandboxes.resolve(name)):
                return await fn(*args, **kwargs)
        except Exception as e:
            return f"Error: {e}"

    return wrapper


@mcp.resource("icedos://config")
@_on_default_root
async def resource_config() -> str:
    """The full merged config (config.toml + every enabled configs/*.toml)."""
    return get_config_toml_string()


@mcp.resource("icedos://config/files")
@_on_default_root
async def resource_config_files() -> str:
    """The loaded config file set — config.toml + enabled configs/*.toml, in
    merge order, as config-root-relative paths."""
    root = find_config_root()
    return json.dumps([str(p.relative_to(root)) for p in config_files.list_config_files(root)], indent=2)


@mcp.resource("icedos://options")
@_on_default_root
async def resource_options() -> str:
    """All IceDOS options with types, descriptions, and values."""
    await _ensure_search_index()
    return json.dumps(_load_index("options-doc.json"), indent=2)


@mcp.resource("icedos://modules")
@_on_default_root
async def resource_modules() -> str:
    """Full module graph across all repositories."""
    await _ensure_search_index()
    return json.dumps(_load_index("modules-doc.json"), indent=2)


@mcp.resource("icedos://generations")
async def resource_generations() -> str:
    """System generations list."""
    return json.dumps(await get_generations(), indent=2)


@mcp.resource("icedos://system-info")
async def resource_system_info() -> str:
    """Current system state."""
    return json.dumps(await get_system_info(), indent=2)


@mcp.resource("icedos://repo/{url}")
async def resource_repo(url: str) -> str:
    """Module metadata for a specific repository.

    The URL is percent-encoded in the URI (e.g. 'github%3Aicedos%2Fapps');
    decode it before handing off to the GitHub explorer.
    """
    try:
        return json.dumps(await explore_github_repo(unquote(url)), indent=2)
    except Exception as e:
        return f"Error: {e}"


# --- PROMPTS ---

@mcp.prompt()
def configure_module(module_name: str, module_url: str = "") -> str:
    """Step-by-step guide to configure an IceDOS module."""
    return get_prompt("configure_module", module_name=module_name, module_url=module_url or "github:icedos/apps")


@mcp.prompt()
def troubleshoot(issue: str = "unknown issue") -> str:
    """Diagnostic workflow for common IceDOS issues."""
    return get_prompt("troubleshoot", issue=issue)


@mcp.prompt()
def add_application(app_name: str) -> str:
    """Guide to add a new application to the system."""
    return get_prompt("add_application", app_name=app_name)


@mcp.prompt()
def icedos_overview() -> str:
    """Overview of IceDOS architecture and capabilities."""
    return get_prompt("icedos_overview")


@mcp.tool()
@_sandboxable
async def nix_eval(expression: str, raw: bool = False) -> str:
    """Evaluate a Nix expression against the same context as `icedos repl`.

    For reading a single option prefer `get_config_value` / `get_option` — they
    answer from the index without spawning Nix. Use this when you need something
    the index cannot express: cross-referencing options, calling `icedosLib`, or
    inspecting `pkgs`/`inputs`.

    Bindings (core/lib/repl-context.nix):
        config     evaluated config of the running system, module defaults included
        options    option declarations — type, default, description
        declared   raw config.toml merge from the working tree (pending changes)
        pkgs       the system's nixpkgs, with its overlays and nixpkgs.config
        lib        nixpkgs lib
        icedosLib  icedos helper library
        inputs     resolved flake inputs

    Evaluation is impure and unsandboxed — it can read any file the server user
    can. Requires a prior rebuild (it reads the generated `.state` flake).

    Examples:
        expression="config.icedos.system.cache.url"
        expression="builtins.attrNames inputs"
        expression="lib.filterAttrs (n: v: v.enable) config.icedos.users"

    Args:
        expression: Nix expression to evaluate.
        raw: Emit the value with `--raw` (strings only) instead of JSON.
    """
    try:
        exit_code, stdout, stderr = await cli_nix_eval_expr(expression, raw=raw)
    except (FileNotFoundError, TimeoutError) as e:
        return _err("nix_eval", str(e))
    if exit_code != 0:
        return _err("nix_eval", (stderr or stdout).strip())
    return stdout


# ===========================================================================

def _drop_unused_default() -> None:
    """Delete the default sandbox on exit when it holds no changes worth reviewing."""
    global _default_name
    if not _default_name:
        return
    try:
        if not sandboxes.diff(_default_name):
            sandboxes.delete(_default_name)
            _default_name = None
    except Exception as e:
        logger.warning("could not clean default sandbox %s: %s", _default_name, e)


def _on_exit() -> None:
    _drop_unused_default()
    try:
        for host, msg in remote.cleanup(timeout=60).items():
            logger.info("remote cleanup %s: %s", host, msg)
    except Exception as e:
        logger.warning("remote cleanup failed: %s", e)


def main() -> None:
    """Entry point."""
    global _live
    parser = argparse.ArgumentParser(prog="icedos-mcp-server")
    _ = parser.add_argument(
        "--live",
        action="store_true",
        help=f"run calls without sandbox= on the real config (same as {LIVE_ENV}=1)",
    )
    _live = _live or parser.parse_args().live
    logger.info("mode: %s", "live" if _live else "sandbox")
    atexit.register(_on_exit)
    # sys.exit would hang on the stdio reader thread, so clean up and leave directly.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_on_exit(), os._exit(0)))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
