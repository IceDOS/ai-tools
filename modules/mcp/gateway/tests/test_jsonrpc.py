"""JSON-RPC helpers."""

import unittest

from mcp.types import INTERNAL_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR

from icedos_mcp_gateway import jsonrpc


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "method": "m"}), jsonrpc.REQUEST)
        self.assertEqual(jsonrpc.classify({"jsonrpc": "2.0", "method": "m"}), jsonrpc.NOTIFICATION)
        self.assertEqual(jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "result": {}}), jsonrpc.RESPONSE)
        self.assertEqual(jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "error": {}}), jsonrpc.RESPONSE)
        self.assertIsNone(jsonrpc.classify({"jsonrpc": "2.0"}))
        self.assertIsNone(jsonrpc.classify({"id": 1, "method": "m"}))
        self.assertIsNone(jsonrpc.classify([]))
        self.assertIsNone(jsonrpc.classify("nope"))

    def test_id_none_is_a_request_not_a_notification(self):
        message = {"jsonrpc": "2.0", "id": None, "method": "m"}
        self.assertTrue(jsonrpc.is_request(message))
        self.assertFalse(jsonrpc.is_notification(message))

    def test_builders(self):
        error = jsonrpc.error_response(None, PARSE_ERROR, "Parse error")
        self.assertEqual(error, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        self.assertEqual(jsonrpc.response(7, {"a": 1}), {"jsonrpc": "2.0", "id": 7, "result": {"a": 1}})
        self.assertEqual(
            jsonrpc.request("gw-1", "ping"), {"jsonrpc": "2.0", "id": "gw-1", "method": "ping"}
        )
        self.assertEqual(
            jsonrpc.notification("notifications/initialized"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    def test_constants_match_the_sdk(self):
        self.assertEqual(jsonrpc.PARSE_ERROR, PARSE_ERROR)
        self.assertEqual(jsonrpc.INVALID_REQUEST, INVALID_REQUEST)
        self.assertEqual(jsonrpc.METHOD_NOT_FOUND, METHOD_NOT_FOUND)
        self.assertEqual(jsonrpc.INTERNAL_ERROR, INTERNAL_ERROR)


if __name__ == "__main__":
    unittest.main()
