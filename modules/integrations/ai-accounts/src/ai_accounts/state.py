"""Sticky defaults and the logged-in cache alimit's views read."""
import json
import os
from pathlib import Path

from . import config


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def sticky() -> dict:
    return _read(config.state_dir() / "sticky.json")


def set_sticky(agent: str, name: str) -> None:
    data = sticky()
    data[agent] = name
    _write(config.state_dir() / "sticky.json", data)


def status() -> dict:
    return _read(config.state_dir() / "status.json")


def write_status(agent: str, logged: dict) -> None:
    data = status()
    data[agent] = logged
    _write(config.state_dir() / "status.json", data)
