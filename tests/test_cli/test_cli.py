"""Tests for RepoGuard CLI."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from cli.main import main, build_parser


class TestCLI(unittest.TestCase):
    """Test CLI commands and flags."""

    def test_parser_help(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    @patch("cli.main.RepoGuardEngine")
    def test_status_command(self, mock_engine_cls):
        mock_engine = MagicMock()
        mock_engine.get_status.return_value = {
            "version": "1.0.0",
            "status": "ready",
            "ollama_enabled": False,
            "ollama_available": False,
            "sandbox_enabled": False,
            "sandbox_available": False,
        }
        mock_engine_cls.return_value = mock_engine

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = main(["status"])
            self.assertEqual(code, 0)
            output = mock_out.getvalue()
            self.assertIn("RepoGuard Engine Status", output)
            self.assertIn("1.0.0", output)

    @patch("cli.main.RepoGuardEngine")
    def test_diagnostics_command(self, mock_engine_cls):
        mock_engine = MagicMock()
        mock_engine.config.db_path = ":memory:"
        mock_engine.config.github_token = None
        mock_engine._get_rate_limit_safe.return_value = None
        mock_engine.sandbox.is_available.return_value = False
        mock_engine_cls.return_value = mock_engine

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = main(["diagnostics"])
            self.assertEqual(code, 0)
            output = mock_out.getvalue()
            self.assertIn("RepoGuard System Diagnostics", output)
            self.assertIn("Fallback Mode", output)

    def test_evaluate_command(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = main(["evaluate"])
            self.assertEqual(code, 0)
            output = mock_out.getvalue()
            self.assertIn("RepoGuard Evaluation Benchmark Results", output)


if __name__ == "__main__":
    unittest.main()

