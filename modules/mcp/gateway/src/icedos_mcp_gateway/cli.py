"""Command line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
import sys

import uvicorn

from .app import create_app
from .config import DEFAULT_HOST, ConfigError, GatewayConfig, load

log = logging.getLogger(__name__)


class GatewayServer(uvicorn.Server):
    """Shut down on SIGTERM instead of re-raising it once the server stopped."""

    def handle_exit(self, sig, frame=None) -> None:  # noqa: ANN001 - uvicorn signature
        self.should_exit = True


def is_loopback(host: str) -> bool:
    """True when every address the host resolves to is a loopback address."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address.split("%")[0]).is_loopback:
                return False
        except ValueError:
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icedos-mcp-gateway",
        description="Serve configured stdio MCP servers over streamable HTTP.",
    )
    parser.add_argument("--config", required=True, help="path to the JSON configuration")
    parser.add_argument("--host", help="override the configured host")
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument("--log-level", default="info", help="logging level (default: info)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config: GatewayConfig = load(args.config)
    except ConfigError as exc:
        print(f"icedos-mcp-gateway: {exc}", file=sys.stderr)
        return 2

    host = args.host or config.host or DEFAULT_HOST
    port = args.port or config.port
    if not is_loopback(host):
        print(
            f"icedos-mcp-gateway: refusing to listen on {host!r}: only loopback hosts are allowed",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    app = create_app(config)
    server = GatewayServer(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
        )
    )
    server.run()
    return 0
