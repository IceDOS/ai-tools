"""Configuration parsing and validation."""

import json
import tempfile
import unittest
from pathlib import Path

from icedos_mcp_gateway.config import ConfigError, load, parse


class ParseTest(unittest.TestCase):
    def test_minimal_server(self):
        config = parse(
            {
                "servers": {
                    "fake": {"mode": "per-agent", "command": "/bin/true", "args": ["a"], "env": {"K": "V"}}
                }
            }
        )
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 7800)
        self.assertEqual(config.idle_timeout, 600.0)
        spec = config.servers["fake"]
        self.assertEqual((spec.name, spec.mode, spec.command), ("fake", "per-agent", "/bin/true"))
        self.assertEqual(spec.args, ["a"])
        self.assertEqual(spec.env, {"K": "V"})

    def test_full_document(self):
        config = parse(
            {"host": "icedos-mcps", "port": 7801, "idleTimeoutSec": 30, "servers": {}}
        )
        self.assertEqual((config.host, config.port, config.idle_timeout), ("icedos-mcps", 7801, 30.0))

    def test_idle_timeout_zero_is_allowed(self):
        self.assertEqual(parse({"idleTimeoutSec": 0}).idle_timeout, 0.0)

    def test_rejects_bad_documents(self):
        cases = [
            [],
            {"port": 0},
            {"port": 70000},
            {"port": True},
            {"port": "7800"},
            {"host": ""},
            {"idleTimeoutSec": -1},
            {"idleTimeoutSec": "600"},
            {"servers": []},
            {"servers": {"bad name": {"mode": "global", "command": "x"}}},
            {"servers": {"fake": {"mode": "stdio", "command": "x"}}},
            {"servers": {"fake": {"mode": "global"}}},
            {"servers": {"fake": {"mode": "global", "command": "x", "args": [1]}}},
            {"servers": {"fake": {"mode": "global", "command": "x", "env": {"K": 1}}}},
        ]
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(ConfigError):
                    parse(document)


class LoadTest(unittest.TestCase):
    def test_loads_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"port": 7810, "servers": {}}))
            self.assertEqual(load(str(path)).port, 7810)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load("/nonexistent/mcp-gateway.json")

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            with self.assertRaises(ConfigError):
                load(str(path))
