"""Configuration loading and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MODES = ("global", "per-agent")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7800
DEFAULT_IDLE_TIMEOUT = 600.0


class ConfigError(ValueError):
    """Raised when the configuration is missing, unreadable or invalid."""


@dataclass
class ServerSpec:
    """One stdio MCP server the gateway fronts."""

    name: str
    mode: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class GatewayConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    servers: dict[str, ServerSpec] = field(default_factory=dict)


def load(path: str) -> GatewayConfig:
    """Read and validate a JSON configuration file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse(data)


def parse(data: Any) -> GatewayConfig:
    """Validate a decoded configuration document."""
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a JSON object")

    host = data.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("host must be a non-empty string")

    port = data.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"port must be an integer between 1 and 65535, got {port!r}")

    idle = data.get("idleTimeoutSec", DEFAULT_IDLE_TIMEOUT)
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or idle < 0 or idle != idle:
        raise ConfigError(f"idleTimeoutSec must be a number >= 0, got {idle!r}")

    servers_raw = data.get("servers", {})
    if not isinstance(servers_raw, dict):
        raise ConfigError("servers must be an object")

    servers: dict[str, ServerSpec] = {}
    for name, entry in servers_raw.items():
        if not isinstance(name, str) or not SERVER_NAME_RE.match(name):
            raise ConfigError(f"invalid server name {name!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"server {name!r} must be an object")
        mode = entry.get("mode", "global")
        if mode not in MODES:
            raise ConfigError(
                f"server {name!r} has mode {mode!r}, expected one of {', '.join(MODES)}"
            )
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"server {name!r} needs a non-empty command")
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ConfigError(f"server {name!r} args must be a list of strings")
        env = entry.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise ConfigError(f"server {name!r} env must map strings to strings")
        servers[name] = ServerSpec(
            name=name, mode=mode, command=command, args=list(args), env=dict(env)
        )

    return GatewayConfig(
        host=host, port=port, idle_timeout=float(idle), servers=servers
    )
