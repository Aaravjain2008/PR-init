"""Tests for Chrome Native Messaging Host logic."""

import io
import json
import struct
import unittest
from unittest.mock import MagicMock, patch

from repoguard.native_host import read_message, send_message, handle_message
from repoguard.models import ScanResult, ItemResult, ItemType


class TestNativeHost(unittest.TestCase):
    """Test native host protocol parsing and message validation."""

    def test_send_and_read_message(self):
        payload = {"action": "get_status"}
        encoded = json.dumps(payload).encode("utf-8")
        raw = struct.pack("@I", len(encoded)) + encoded

        with patch("sys.stdin.buffer.read") as mock_read:
            # 1st call length, 2nd call body
            mock_read.side_effect = [raw[:4], raw[4:]]
            msg = read_message()
            self.assertEqual(msg, payload)

    def test_handle_status_action(self):
        engine = MagicMock()
        engine.get_status.return_value = {"status": "ready"}
        res = handle_message({"action": "get_status"}, engine)
        self.assertEqual(res["status"], "ready")

    def test_unauthorized_action_rejected(self):
        engine = MagicMock()
        res = handle_message({"action": "exec_shell", "cmd": "rm -rf /"}, engine)
        self.assertIn("error", res)
        self.assertIn("Invalid action", res["error"])

    def test_scan_item_validation(self):
        engine = MagicMock()
        # Missing number
        res = handle_message({"action": "scan_item", "repository": "owner/repo"}, engine)
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()

