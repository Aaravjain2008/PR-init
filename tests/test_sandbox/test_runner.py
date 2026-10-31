"""Tests for the Sandboxed Execution Runner."""

import unittest
from unittest.mock import MagicMock, patch

from repoguard.sandbox.runner import (
    SandboxRunner,
    SandboxResult,
    SANDBOX_TIMEOUT_SECONDS,
    SANDBOX_MEMORY_LIMIT,
    SANDBOX_CPU_LIMIT,
    SANDBOX_PIDS_LIMIT,
)


class TestSandboxRunner(unittest.TestCase):
    """Test safety constraints and execution of SandboxRunner."""

    def test_disabled_by_default(self):
        runner = SandboxRunner(enabled=False)
        self.assertFalse(runner.is_available())

    def test_fallback_when_docker_missing(self):
        runner = SandboxRunner(enabled=True)
        with patch("shutil.which", return_value=None):
            self.assertFalse(runner.is_available())
            res = runner.run("echo hello")
            self.assertFalse(res.success)
            self.assertEqual(res.sandbox_type, "none")
            self.assertIn("Sandbox unavailable", res.error)
            # Crucial: command was NEVER executed on the host
            self.assertEqual(res.exit_code, None)

    def test_fallback_when_docker_daemon_not_running(self):
        runner = SandboxRunner(enabled=True)
        with patch("shutil.which", return_value="/usr/local/bin/docker"), \
             patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1, stderr="Cannot connect to daemon")
            self.assertFalse(runner.is_available())

    @patch("shutil.which", return_value="/usr/local/bin/docker")
    @patch("subprocess.run")
    def test_docker_run_security_flags(self, mock_sub, mock_which):
        # 1st call is docker info
        mock_info = MagicMock(returncode=0)
        # 2nd call is docker run
        mock_run = MagicMock(returncode=0, stdout="test output", stderr="")
        mock_sub.side_effect = [mock_info, mock_run]

        runner = SandboxRunner(enabled=True)
        res = runner.run("pytest test_something.py", repo_path="/tmp/fake_repo")

        self.assertTrue(res.success)
        self.assertEqual(res.stdout, "test output")

        # Inspect the docker run invocation arguments for security flags
        run_args = mock_sub.call_args[0][0]
        self.assertIn("--network=none", run_args)
        self.assertIn("--read-only", run_args)
        self.assertIn("--user=nobody", run_args)
        self.assertIn("--security-opt=no-new-privileges", run_args)
        self.assertIn(f"--memory={SANDBOX_MEMORY_LIMIT}", run_args)
        self.assertIn(f"--cpus={SANDBOX_CPU_LIMIT}", run_args)
        self.assertIn(f"--pids-limit={SANDBOX_PIDS_LIMIT}", run_args)
        # Verify read-only mount
        self.assertIn("/tmp/fake_repo:/repo:ro", run_args)

    @patch("shutil.which", return_value="/usr/local/bin/docker")
    @patch("subprocess.run")
    def test_timeout_handling(self, mock_sub, mock_which):
        import subprocess
        mock_info = MagicMock(returncode=0)
        mock_sub.side_effect = [mock_info, subprocess.TimeoutExpired(cmd="docker run", timeout=10)]

        runner = SandboxRunner(enabled=True)
        res = runner.run("sleep 100", timeout=10)

        self.assertFalse(res.success)
        self.assertIn("timed out after 10 seconds", res.error)


if __name__ == "__main__":
    unittest.main()

