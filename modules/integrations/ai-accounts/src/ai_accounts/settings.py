"""Per-account client settings: module-managed keys, then the account's overrides, merged into the agent's file."""
import json
import os
from pathlib import Path

ABSENT = {"__ai_account__": "absent"}


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def flatten(tree: dict, prefix=()) -> dict:
    """Leaf paths of nested dicts; lists, scalars, None and empty dicts are leaves."""
    out = {}
    for k, v in tree.items():
        path = prefix + (k,)
        if isinstance(v, dict) and v:
            out.update(flatten(v, path))
        else:
            out[path] = v
    return out


def _get(data, path):
    for k in path:
        if not isinstance(data, dict) or k not in data:
            return ABSENT
        data = data[k]
    return data


def _put(data: dict, path, value) -> None:
    for k in path[:-1]:
        if not isinstance(data.get(k), dict):
            data[k] = {}
        data = data[k]
    if value == ABSENT:
        data.pop(path[-1], None)
    else:
        data[path[-1]] = value


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ai-account.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def apply(target: Path, desired: dict, ledger: Path) -> bool:
    """Merge desired leaves into target; undo leaves dropped since the last run. True if target changed."""
    try:
        data = _read(target, {})
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    try:
        book = _read(ledger, {})
    except ValueError:
        book = {}
    before = json.dumps(data, sort_keys=True)
    want = {json.dumps(p): (ABSENT if v is None else v) for p, v in flatten(desired).items()}
    for key in [k for k in book if k not in want]:
        path, rec = tuple(json.loads(key)), book.pop(key)
        # Restore only what ai-account still owns; a value the agent changed since stays.
        if _get(data, path) == rec["set"]:
            _put(data, path, rec["was"])
    for key, value in want.items():
        path = tuple(json.loads(key))
        rec = book.setdefault(key, {"was": _get(data, path)})
        rec["set"] = value
        _put(data, path, value)
    changed = json.dumps(data, sort_keys=True) != before
    if changed:
        _write(target, data)
    if book != _read_or_empty(ledger):
        _write(ledger, book)
    return changed


def _read_or_empty(path: Path) -> dict:
    try:
        return _read(path, {})
    except ValueError:
        return {}
