"""Tests for RepoGuard CLI."""

import io
import json
import os
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
            self.assertIn("Synthetic Regression Suite", output)
            self.assertIn("Synthetic cases; not a measure of accuracy on real GitHub issues.", output)

    @patch("cli.main.RepoGuardEngine")
    @patch("cli.main.APIServer")
    def test_serve_command_output(self, mock_server_cls, mock_engine_cls):
        mock_server = MagicMock()
        mock_server.secret = "abc123secret456"
        mock_server.start.side_effect = KeyboardInterrupt
        mock_server_cls.return_value = mock_server

        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main(["serve"])
                self.assertEqual(code, 0)
                output = mock_out.getvalue()
                self.assertIn("abc123secret456", output)
                self.assertIn("Pairing secret:", output)
                self.assertIn("Dashboard URL:", output)
                self.assertIn("Stored in", output)
                self.assertNotIn("development mode", output)
                self.assertNotIn("Warning: Running without pairing secret", output)

    @patch("cli.main.RepoGuardEngine")
    @patch("cli.main.APIServer")
    def test_serve_command_explicit_secret(self, mock_server_cls, mock_engine_cls):
        mock_server = MagicMock()
        mock_server.secret = "mine123"
        mock_server.start.side_effect = KeyboardInterrupt
        mock_server_cls.return_value = mock_server

        # Test with --secret flag
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main(["serve", "--secret", "mine123"])
                self.assertEqual(code, 0)
                output = mock_out.getvalue()
                self.assertIn("Using the pairing secret you provided (not stored on disk).", output)
                self.assertNotIn("mine123", output)
                self.assertNotIn("Stored in", output)
                self.assertNotIn("Pairing secret:", output)

        # Test with REPOGUARD_SECRET environment variable
        with patch.dict(os.environ, {"REPOGUARD_SECRET": "envsecret456"}):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main(["serve"])
                self.assertEqual(code, 0)
                output = mock_out.getvalue()
                self.assertIn("Using the pairing secret you provided (not stored on disk).", output)
                self.assertNotIn("envsecret456", output)
                self.assertNotIn("Stored in", output)
                self.assertNotIn("Pairing secret:", output)


if __name__ == "__main__":
    unittest.main()

