"""Sandboxed execution for safe reproduction of technical claims.

Untrusted code from unknown contributors must never run on the host.
When a container runtime is unavailable, falls back to static verification
and reports "not reproduced: sandbox unavailable".
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Resource limits for sandboxed execution
SANDBOX_TIMEOUT_SECONDS = 30
SANDBOX_MEMORY_LIMIT = "256m"
SANDBOX_CPU_LIMIT = "0.5"
SANDBOX_PIDS_LIMIT = 64


@dataclass
class SandboxResult:
    """Result of a sandboxed execution attempt."""
    success: bool
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    command: str = ""
    duration_seconds: float = 0.0
    sandbox_type: str = "none"  # "docker", "none"
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:10000],  # Cap output size
            "stderr": self.stderr[:10000],
            "command": self.command,
            "duration_seconds": self.duration_seconds,
            "sandbox_type": self.sandbox_type,
            "error": self.error,
        }


class SandboxRunner:
    """Runs commands in an isolated, disposable sandbox.
    
    Security properties:
    - No network access by default
    - No host filesystem mounts except read-only repo copy
    - No credentials or environment secrets
    - CPU, memory, process and time limits
    - Non-root user
    - Automatic teardown
    
    Falls back gracefully when container runtime is unavailable.
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        enabled: bool = False,
    ):
        self.image = image
        self.enabled = enabled
        self._docker_available: Optional[bool] = None

    def is_available(self) -> bool:
        """Check if Docker is available for sandboxed execution."""
        if not self.enabled:
            return False
        if self._docker_available is not None:
            return self._docker_available
        
        docker_path = shutil.which("docker")
        if not docker_path:
            self._docker_available = False
            logger.info("Docker not found in PATH; sandbox unavailable")
            return False
        
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
                text=True,
            )
            self._docker_available = result.returncode == 0
            if not self._docker_available:
                logger.info("Docker not operational; sandbox unavailable")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            self._docker_available = False
            logger.info("Docker check failed; sandbox unavailable")
        
        return self._docker_available

    def run(
        self,
        command: str,
        repo_path: Optional[str] = None,
        timeout: int = SANDBOX_TIMEOUT_SECONDS,
    ) -> SandboxResult:
        """Execute a command in a sandboxed environment.
        
        Args:
            command: The command to run inside the sandbox.
            repo_path: Optional path to repository to mount read-only.
            timeout: Maximum execution time in seconds.
            
        Returns:
            SandboxResult with execution details.
            
        The command is NEVER run on the host, even if Docker is unavailable.
        """
        if not self.is_available():
            return SandboxResult(
                success=False,
                command=command,
                sandbox_type="none",
                error="Sandbox unavailable: container runtime not found. "
                      "Install Docker to enable sandboxed reproduction.",
            )

        return self._run_docker(command, repo_path, timeout)

    def _run_docker(
        self,
        command: str,
        repo_path: Optional[str],
        timeout: int,
    ) -> SandboxResult:
        """Run a command in a Docker container with strict isolation."""
        docker_cmd = [
            "docker", "run",
            "--rm",                              # Auto-remove container
            "--network=none",                    # No network access
            f"--memory={SANDBOX_MEMORY_LIMIT}",  # Memory limit
            f"--cpus={SANDBOX_CPU_LIMIT}",       # CPU limit
            f"--pids-limit={SANDBOX_PIDS_LIMIT}",# Process limit
            "--read-only",                       # Read-only root filesystem
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",  # Writable /tmp
            "--user=nobody",                     # Non-root user
            "--security-opt=no-new-privileges",  # No privilege escalation
        ]

        if repo_path:
            # Mount repo as read-only
            docker_cmd.extend(["-v", f"{repo_path}:/repo:ro"])
            docker_cmd.extend(["-w", "/repo"])

        docker_cmd.extend([self.image, "sh", "-c", command])

        start_time = time.monotonic()
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.monotonic() - start_time
            return SandboxResult(
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                command=command,
                duration_seconds=round(duration, 2),
                sandbox_type="docker",
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start_time
            return SandboxResult(
                success=False,
                command=command,
                duration_seconds=round(duration, 2),
                sandbox_type="docker",
                error=f"Execution timed out after {timeout} seconds",
            )
        except Exception as e:
            duration = time.monotonic() - start_time
            return SandboxResult(
                success=False,
                command=command,
                duration_seconds=round(duration, 2),
                sandbox_type="docker",
                error=f"Sandbox execution error: {str(e)}",
            )

