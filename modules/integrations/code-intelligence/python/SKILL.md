---
name: python
description: Python language guidance and tooling (uv, pip, venv, typing, testing). Use when writing, editing, or debugging Python code, setting up dependencies, or running linters/type checkers.
---

# Python

Practical guidance for Python development on this system.

## Environment & tooling
- Python 3.11 is the system interpreter. Prefer `uv` for dependency management and virtual environments.
- Create a venv: `uv venv`; activate: `source .venv/bin/activate`. Install packages: `uv pip install <pkg>`.
- Run a project through its own interpreter from the repo root (e.g. `.venv/bin/python ...`), not the system python.
- Linters/type checkers (available via `nix-shell -p pyright ruff mypy --run "..."` if not on PATH):
  - `ruff check .` — fast linting and formatting
  - `mypy .` — static type checking
  - `pyright .` — type checking (strict-friendly)
- Prefer `ruff format` for formatting; keep line length reasonable (88 default).

## Style & conventions
- Follow PEP 8. Use snake_case for functions/variables, CamelCase for classes, UPPER_CASE for constants.
- Type hints on all public functions and parameters. Use `from __future__ import annotations` for forward refs.
- Prefer `dataclasses` over hand-written `__init__` for data containers.
- Use `pathlib.Path` for filesystem access, not `os.path` string surgery.
- Use `async`/`await` for I/O-bound concurrency; `asyncio.gather` for parallel awaits.
- Prefer exceptions with clear messages over silent `pass` or bare `except:`.

## Common patterns
- Virtualenv bootstrap in a module/derivation: `python311` from nixpkgs, then `uv pip install` into a venv.
- Read files: `Path("x").read_text()` / `.write_text()`. Parse config: `tomllib` (3.11+) or `tomli`.
- Env-driven config: `os.environ.get("NAME", default)`; `python-dotenv` for `.env` files.
- Iterate with `for x in items:`; when index needed use `enumerate(items)`.

## Gotchas
- Mutating a list while iterating it causes subtle bugs; iterate over a copy `list(items)`.
- Default mutable args (`def f(x=[])`) are shared across calls — use `None` + guard.
- `is` compares identity, `==` compares value; use `is None`, not `== None`.
- F-strings: `f"{value}"`; embed expressions, not full statements.
- Watch for stale `.pyc` after edits; Python recompiles automatically on mtime change.

## Testing
- Prefer `pytest`; name tests `test_*.py` with `test_*` functions.
- Run `pytest -q` for a quiet run; `pytest -x` stops at first failure.
- Use `tmp_path` fixture for temp files; `monkeypatch` for env/function patching.
