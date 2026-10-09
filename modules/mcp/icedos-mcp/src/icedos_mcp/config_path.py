"""Dotted-path parsing and lookup shared by config.py and config_files.py.

Kept separate so config_files.py can use these helpers without importing
config.py (which imports config_files.py), avoiding an import cycle.
"""

from __future__ import annotations

import re
from typing import Any

APPEND = object()  # sentinel for the `path[]` append accessor (set only)
_SEG = re.compile(r"^([^\[\]]*)((?:\[[^\]]*\])*)$")
_IDX = re.compile(r"\[([^\]]*)\]")


def parse_path(dotted_path: str) -> list[Any]:
    """Tokenize a dotted path with optional `[N]` / `[]` accessors.

    'icedos.repositories[0].modules[1]' -> ['icedos','repositories',0,'modules',1]
    'icedos.system.packages[]'          -> ['icedos','system','packages',APPEND]
    Bare keys still parse to plain string tokens (backward compatible).
    """
    tokens: list[Any] = []
    for seg in dotted_path.split("."):
        m = _SEG.match(seg)
        if not m:
            raise ValueError(f"invalid path segment '{seg}' in '{dotted_path}'")
        base, brackets = m.group(1), m.group(2)
        if base:
            tokens.append(base)
        elif not brackets:
            raise ValueError(f"empty path segment in '{dotted_path}'")
        for raw in _IDX.findall(brackets):
            raw = raw.strip()
            if raw == "":
                tokens.append(APPEND)
            else:
                try:
                    tokens.append(int(raw))
                except ValueError:
                    raise ValueError(f"invalid array index '[{raw}]' in '{dotted_path}'")
    if any(t is APPEND for t in tokens[:-1]):
        raise ValueError(f"'[]' append is only valid as the final accessor in '{dotted_path}'")
    return tokens


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
