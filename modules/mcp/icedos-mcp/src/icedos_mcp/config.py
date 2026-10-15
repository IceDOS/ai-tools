"""Config read/edit operations using tomlkit for round-trip preservation.

User config is a *set* of files: ``config.toml`` (global base) plus every enabled
``*.toml`` under the ``icedos.system.extraConfigs`` dirs (default ``configs/``) —
see ``config_files.py`` (the Python mirror of core ``lib/config-files.nix``).

Reads default to the *merged* view across all files; writes target the file that
already declares the key (or ``config.toml`` for new keys), so edits land in the
right place instead of shadowing a value that lives in ``configs/*.toml``. A
specific file can always be forced with the ``file`` argument (e.g. write a secret
into a hidden ``configs/.local.toml``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import dumps, loads

from . import config_files


def _normalize_blank_lines(content: str) -> str:
    """Ensure exactly one blank line before every table header.

    tomlkit appends newly-created tables (new repos, users, sections) with no
    leading blank line, jamming them against the preceding block. This restores
    the one-blank-line-between-blocks style the hand-written configs use. It is
    idempotent: unchanged, correctly-spaced files round-trip untouched. Only
    column-0 `[`/`[[` header lines are affected; indented array rows are not.
    """
    out: list[str] = []
    for line in content.split("\n"):
        if line.startswith("[") and out:
            while out and out[-1].strip() == "":
                out.pop()
            if out:
                out.append("")
        out.append(line)
    return "\n".join(out)


def _coerce_candidates(raw: str) -> list[Any]:
    """Interpretations of a string arg to match against typed array items.

    An array-item delete arrives as a string; a `[1, 2]` array holds ints, so
    try bool/int/float forms in addition to the literal string.
    """
    candidates: list[Any] = [raw]
    low = raw.lower()
    if low in ("true", "false"):
        candidates.append(low == "true")
    try:
        candidates.append(int(raw))
    except ValueError:
        pass
    try:
        candidates.append(float(raw))
    except ValueError:
        pass
    return candidates


def _is_config_root(p: Path) -> bool:
    """A config root's flake.nix calls mkIceDOS, the same test core's rebuild shim
    uses. Module repos also hold flake.nix and modules/, so that alone is not enough."""
    try:
        return "mkIceDOS" in (p / "flake.nix").read_text()
    except (OSError, UnicodeDecodeError):
        return False


# Per call, not per server, so concurrent calls can each target their own sandbox.
_root_override: ContextVar[Path | None] = ContextVar("icedos_root_override", default=None)


def root_override() -> Path | None:
    """The sandbox root the current call runs against, or None for the live config."""
    return _root_override.get()


@contextmanager
def use_config_root(root: Path) -> Iterator[None]:
    """Point find_config_root() at `root` for the duration of the block."""
    token = _root_override.set(root)
    try:
        yield
    finally:
        _root_override.reset(token)


def find_config_root() -> Path:
    """Find the IceDOS config root by walking up from cwd for a flake.nix root
    (config.toml is optional). Respects the ICEDOS_CONFIG_DIR env var if set.
    A sandbox set with use_config_root() wins over both.
    """
    override = _root_override.get()
    if override is not None:
        return override
    return live_config_root()


def live_config_root() -> Path:
    """The real config root, ignoring any sandbox set for the current call."""
    env_dir = os.environ.get("ICEDOS_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if (p / "flake.nix").exists():
            return p
        raise FileNotFoundError(f"ICEDOS_CONFIG_DIR={env_dir} does not contain flake.nix")

    current = Path.cwd()
    for parent in [current, *current.parents]:
        if _is_config_root(parent):
            return parent
    raise FileNotFoundError(
        "Cannot find IceDOS config root (a flake.nix that calls mkIceDOS). "
        "Set ICEDOS_CONFIG_DIR or run from within an IceDOS config directory."
    )


_read_plain = config_files._read


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    """Round-trip tomlkit document for a specific file; empty doc if it does not
    exist yet (so writing to a new ``configs/<name>.toml`` creates it)."""
    if not path.exists():
        return tomlkit.document()
    try:
        return loads(path.read_text())
    except Exception as e:
        raise ValueError(f"invalid TOML in '{path}': {e}") from None


def _save(target: Path, doc: tomlkit.TOMLDocument) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_normalize_blank_lines(dumps(doc)))


def rel(path: Path) -> str:
    """``path`` relative to the config root, for messages."""
    root = find_config_root().resolve()
    p = path.resolve()
    return p.relative_to(root).as_posix() if p.is_relative_to(root) else str(path)


def read_config(file: str | None = None) -> dict[str, Any]:
    """Parse config into a dict. ``file=None`` → the merged view across all config
    files; an explicit ``file`` → just that file."""
    root = find_config_root()
    if file is None:
        return config_files.merged_config(root)
    return _read_plain(config_files.resolve_file(root, file))


from .config_path import APPEND as _APPEND
from .config_path import get_by_tokens as _get_by_tokens
from .config_path import parse_path as _parse_path
from .config_path import quote_key


def get_value(dotted_path: str, file: str | None = None) -> Any:
    """Get a value by dotted path (e.g. 'icedos.system.arch').

    ``file=None`` looks it up in the merged config (so values in any
    ``configs/*.toml`` are found); an explicit ``file`` reads just that file.
    Supports array indexing: 'icedos.system.packages[0]',
    'icedos.repositories[0].modules[1]' (negative indices allowed).
    """
    config = read_config(file=file)
    return _get_by_tokens(config, _parse_path(dotted_path), dotted_path)


def _table_add(table: Any, key: str, value: Any) -> None:
    """Add a key to a tomlkit table, handling OutOfOrderTableProxy."""
    try:
        table.add(key, value)
    except AttributeError:
        table[key] = value


def _set_in_doc(doc: Any, tokens: list[Any], value: Any, dotted_path: str) -> None:
    # Missing string-key intermediates are created as arrays or tables per the next token.
    current = doc
    for i, tok in enumerate(tokens[:-1]):
        if isinstance(tok, str):
            if tok not in current:
                nxt = tokens[i + 1]
                new = tomlkit.array() if (nxt is _APPEND or isinstance(nxt, int)) else tomlkit.table()
                _table_add(current, tok, new)
            current = current[tok]
        else:
            try:
                current = current[tok]
            except (IndexError, KeyError, TypeError):
                raise KeyError(f"Path '{dotted_path}' not found in config")

    last = tokens[-1]
    if last is _APPEND:
        if not isinstance(current, list):
            raise TypeError(f"'[]' append target is not an array: '{dotted_path}'")
        current.append(value)
    elif isinstance(last, int):
        if not isinstance(current, list):
            raise TypeError(f"'[{last}]' index target is not an array: '{dotted_path}'")
        try:
            current[last] = value
        except IndexError:
            raise IndexError(f"index [{last}] out of range for '{dotted_path}' (len {len(current)})")
    elif not isinstance(current, dict):
        raise TypeError(f"'{dotted_path}' descends into a {type(current).__name__}, not a table")
    elif last in current:
        current[last] = value
    else:
        _table_add(current, last, value)


def set_value(dotted_path: str, value: Any, file: str | None = None) -> Path:
    """Set a value by dotted path. Returns the file written.

    Without ``file``, the path is read against the merged view: the write lands
    in the file that declares the key, ``[N]`` names the Nth item of the merged
    array (mapped to the file holding it), and ``[]`` appends to the last file
    holding the array. A new key goes to the file declaring its nearest existing
    parent, else ``config.toml``. With ``file``, indices are local to that file.
    """
    root = find_config_root()
    tokens = _parse_path(dotted_path)
    if file:
        target, local = config_files.resolve_file(root, file), tokens
    else:
        loc = config_files.locate(root, tokens)
        appending = tokens[-1] is _APPEND
        if loc.exists and not appending and len(loc.files) > 1:
            raise ValueError(
                f"'{dotted_path}' is merged from several files ({', '.join(rel(f) for f in loc.files)}); "
                "set a narrower path or pass file="
            )
        if loc.files:
            target = loc.files[-1] if appending else loc.files[0]
        else:
            target = root / "config.toml"
        local = loc.local
    doc = _read_doc(target)
    _set_in_doc(doc, local, value, dotted_path)
    _save(target, doc)
    return target


def _remove_item(arr: list[Any], raw: str) -> bool:
    """Drop the first item equal to ``raw`` read as str, bool, int or float, matching types exactly."""
    for candidate in _coerce_candidates(raw):
        for i, item in enumerate(arr):
            v = item.unwrap() if hasattr(item, "unwrap") else item
            if type(v) is type(candidate) and v == candidate:
                del arr[i]
                return True
    return False


def delete_value(dotted_path: str, file: str | None = None) -> list[Path]:
    """Delete a value by dotted path. Returns the files written.

    Supports:
    - Removing a key: 'icedos.system.ssh' (from every file that declares it)
    - Removing an array item by value: 'icedos.system.packages|signal-desktop'
      (the first match, searching the declaring files in load order)
    - Removing an array element by merged index: 'icedos.system.packages[2]'
    With ``file``, only that file is edited and indices are local to it.
    """
    root = find_config_root()
    by_value = "|" in dotted_path
    path, item = dotted_path.split("|", 1) if by_value else (dotted_path, "")
    tokens = _parse_path(path)
    if any(t is _APPEND for t in tokens):
        raise ValueError("'[]' append is not valid for delete")
    if file:
        places = [(config_files.resolve_file(root, file), tokens)]
    else:
        loc = config_files.locate(root, tokens)
        if not loc.exists:
            raise KeyError(f"'{path}' not found in any config file")
        places = [(f, loc.local) for f in loc.files]

    written: list[Path] = []
    for target, local in places:
        doc = _read_doc(target)
        if by_value:
            arr = _get_by_tokens(doc, local, path)
            if not isinstance(arr, list):
                raise TypeError(f"Expected array at '{path}', got {type(arr).__name__}")
            if _remove_item(arr, item):
                _save(target, doc)
                return [target]
            continue
        parent = _get_by_tokens(doc, local[:-1], path)
        last = local[-1]
        if isinstance(last, int):
            if not isinstance(parent, list):
                raise TypeError(f"'[{last}]' index target is not an array: '{path}'")
            try:
                del parent[last]
            except IndexError:
                raise IndexError(f"index [{last}] out of range for '{path}' (len {len(parent)})")
        else:
            if not isinstance(parent, dict) or last not in parent:
                raise KeyError(f"Key '{path}' not found in {rel(target)}")
            del parent[last]
        _save(target, doc)
        written.append(target)
    if by_value:
        raise ValueError(f"Item '{item}' not found in array '{path}'")
    return written


def list_repositories() -> list[dict[str, Any]]:
    """Every repository, one row per repo, folded over all its entries like core.

    Rows key on the base url (ref stripped). Modules and patches concatenate,
    `fetchDependencies = false` and `fetchOptionalDependencies = true` win when
    any entry sets them, and ``files`` lists every file with an entry. An
    override set by sandbox_override_repo shows as ``sandbox_override``.
    """
    root = find_config_root()
    rows: dict[str, dict[str, Any]] = {}
    for f in config_files.list_config_files(root):
        for r in _read_plain(f).get("icedos", {}).get("repositories", []) or []:
            url = r.get("url", "")
            row = rows.setdefault(
                config_files.base_url(url),
                {
                    "url": url,
                    "files": [],
                    "override_url": "",
                    "modules": [],
                    "fetch_dependencies": True,
                    "fetch_optional_dependencies": False,
                    "patches": [],
                    "input_patches": [],
                },
            )
            if f.name == config_files.OVERRIDES_FILE:
                row["sandbox_override"] = r.get("overrideUrl", "")
            else:
                row["files"].append(rel(f))
            if r.get("overrideUrl") and not row["override_url"]:
                row["override_url"] = r["overrideUrl"]
            row["modules"] += r.get("modules", [])
            row["fetch_dependencies"] &= r.get("fetchDependencies", True)
            row["fetch_optional_dependencies"] |= r.get("fetchOptionalDependencies", False)
            row["patches"] += r.get("patches", [])
            row["input_patches"] += r.get("input-patches", [])
    return list(rows.values())


def set_module_enabled(module: str, repo_url: str, enabled: bool, file: str | None = None) -> list[Path]:
    """Add or remove ``module`` in the ``modules`` list of ``repo_url``'s entries.

    Entries match by base url, so ``github:o/r/ref`` and ``github:o/r`` are one
    repo. Enabling appends to the first entry unless any entry already lists it.
    Disabling removes it from every entry. ``file`` limits both to that file.
    Returns the files written.
    """
    root = find_config_root()
    entries = config_files.repo_entries(root, repo_url)
    if file:
        only = config_files.resolve_file(root, file)
        entries = [(f, j) for f, j in entries if f == only]
    if not entries:
        where = f" in {file}" if file else " in any config file"
        raise KeyError(f"repository '{repo_url}' not found{where}")
    holding = [(f, j) for f, j in entries if module in (_read_plain(f)["icedos"]["repositories"][j].get("modules") or [])]

    if enabled:
        if holding:
            raise ValueError(f"module '{module}' already enabled in {repo_url} ({rel(holding[0][0])})")
        f, j = entries[0]
        doc = _read_doc(f)
        entry = doc["icedos"]["repositories"][j]
        if "modules" not in entry:
            entry.add("modules", tomlkit.array())
        entry["modules"].append(module)
        _save(f, doc)
        return [f]

    if not holding:
        raise ValueError(f"module '{module}' not enabled in {repo_url}")
    written: list[Path] = []
    for f in dict.fromkeys(f for f, _ in holding):
        doc = _read_doc(f)
        for g, j in holding:
            if g == f:
                mods = doc["icedos"]["repositories"][j]["modules"]
                while module in mods:
                    mods.remove(module)
        _save(f, doc)
        written.append(f)
    return written


def add_repository(
    url: str,
    modules: list[str] | None = None,
    fetch_optional_deps: bool = False,
    fetch_deps: bool = True,
    override_url: str = "",
    patches: list[str] | None = None,
    input_patches: list[dict[str, Any]] | None = None,
    file: str | None = None,
) -> Path:
    """Add a new [[icedos.repositories]] entry. Returns the file written.

    Appends to the first file that already holds repositories (keeping them
    together), else ``config.toml``, or the explicit ``file``. The duplicate
    check compares base urls across every config file.
    """
    root = find_config_root()
    existing = config_files.repo_entries(root, url)
    if existing:
        raise ValueError(f"Repository '{url}' already exists in {rel(existing[0][0])}")
    for ip in input_patches or []:
        if not isinstance(ip, dict) or not {"module", "input", "patches"} <= ip.keys():
            raise ValueError("each input_patches entry needs module, input and patches")
    if file:
        target = config_files.resolve_file(root, file)
    else:
        target = next(
            (f for f in config_files.writable_files(root) if _read_plain(f).get("icedos", {}).get("repositories")),
            root / "config.toml",
        )

    doc = _read_doc(target)
    if "icedos" not in doc:
        doc.add("icedos", tomlkit.table())
    icedos = doc["icedos"]
    if "repositories" not in icedos:
        icedos.add("repositories", tomlkit.aot())

    entry = tomlkit.table()
    entry.add("url", url)
    if override_url:
        entry.add("overrideUrl", override_url)
    if modules:
        entry.add("modules", list(modules))
    if not fetch_deps:
        entry.add("fetchDependencies", False)
    if fetch_optional_deps:
        entry.add("fetchOptionalDependencies", True)
    if patches:
        entry.add("patches", list(patches))
    if input_patches:
        aot = tomlkit.aot()
        for ip in input_patches:
            aot.append(tomlkit.item(ip))
        entry.add("input-patches", aot)
    icedos["repositories"].append(entry)
    _save(target, doc)
    return target


def edit_user(username: str, settings: dict[str, Any], file: str | None = None) -> list[Path]:
    """Add or update keys of ``[icedos.users.<username>]``. Returns the files written.

    Each key goes through set_value, so it lands in the file that declares it,
    or the file declaring the user, or ``config.toml``. ``file`` forces one file.
    """
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings must be a non-empty JSON object")
    base = f"icedos.users.{quote_key(username)}"
    written: list[Path] = []
    for key, value in settings.items():
        f = set_value(f"{base}.{quote_key(key)}", value, file=file)
        if f not in written:
            written.append(f)
    return written


def get_config_toml_string(file: str | None = None) -> str:
    """Raw config content. ``file=None`` → the merged config serialized to TOML;
    an explicit ``file`` → that file's raw text (comments/formatting preserved)."""
    root = find_config_root()
    if file:
        return config_files.resolve_file(root, file).read_text()
    return dumps(config_files.merged_config(root))
