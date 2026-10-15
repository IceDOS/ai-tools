"""Dotted-path parsing and lookup shared by config.py and config_files.py.

Kept separate so config_files.py can use these helpers without importing
config.py (which imports config_files.py), avoiding an import cycle.
"""

from __future__ import annotations

import json
import re
from typing import Any

APPEND = object()  # sentinel for the `path[]` append accessor (set only)
_BARE = re.compile(r"[^.\[\]\"']+")
_IDX = re.compile(r"\[([^\]]*)\]")


def _key(text: str, i: int, dotted_path: str) -> tuple[str, int]:
    """Read one key at ``text[i]``: bare, "double-quoted" (JSON escapes) or 'literal'."""
    if i < len(text) and text[i] in "\"'":
        quote = text[i]
        j = i + 1
        while j < len(text) and text[j] != quote:
            j += 2 if quote == '"' and text[j] == "\\" else 1
        if j >= len(text):
            raise ValueError(f"unterminated quoted key in '{dotted_path}'")
        raw = text[i + 1 : j]
        if quote == '"':
            raw = json.loads(f'"{raw}"')
        return raw, j + 1
    m = _BARE.match(text, i)
    if not m:
        raise ValueError(f"empty path segment in '{dotted_path}'")
    return m.group(0).strip(), m.end()


def parse_path(dotted_path: str) -> list[Any]:
    """Tokenize a dotted path with optional `[N]` / `[]` accessors.

    'icedos.repositories[0].modules[1]' -> ['icedos','repositories',0,'modules',1]
    'icedos.system.packages[]'          -> ['icedos','system','packages',APPEND]
    'environment.etc."foo.conf".text'   -> ['environment','etc','foo.conf','text']
    Keys holding dots go in TOML-style double or single quotes.
    """
    tokens: list[Any] = []
    i, n = 0, len(dotted_path)
    if n == 0:
        raise ValueError("empty path")
    while True:
        if i < n and dotted_path[i] == "[":
            if not tokens:
                raise ValueError(f"path '{dotted_path}' cannot start with an index")
        else:
            key, i = _key(dotted_path, i, dotted_path)
            tokens.append(key)
        while i < n and dotted_path[i] == "[":
            m = _IDX.match(dotted_path, i)
            if not m:
                raise ValueError(f"unterminated '[' in '{dotted_path}'")
            raw = m.group(1).strip()
            if raw == "":
                tokens.append(APPEND)
            else:
                try:
                    tokens.append(int(raw))
                except ValueError:
                    raise ValueError(f"invalid array index '[{raw}]' in '{dotted_path}'")
            i = m.end()
        if i == n:
            break
        if dotted_path[i] != ".":
            raise ValueError(f"unexpected '{dotted_path[i]}' at position {i} in '{dotted_path}'")
        i += 1
        if i == n:
            raise ValueError(f"empty path segment in '{dotted_path}'")
    if any(t is APPEND for t in tokens[:-1]):
        raise ValueError(f"'[]' append is only valid as the final accessor in '{dotted_path}'")
    return tokens


def quote_key(key: str) -> str:
    """A path segment for ``key``, quoted when it is not a bare key."""
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def get_by_tokens(root: Any, tokens: list[Any], dotted_path: str) -> Any:
    """Descend `root` through parsed tokens (str keys and int indices)."""
    current = root
    for tok in tokens:
        if tok is APPEND:
            raise ValueError("'[]' append is only valid for set operations")
        try:
            current = current[tok]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"Path '{dotted_path}' not found in config")
    return current
