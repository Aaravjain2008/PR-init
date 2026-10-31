"""Tests for APIHandler and server routing without requiring network sockets."""

import io
import json
import unittest
from unittest.mock import MagicMock

from repoguard.api.server import APIHandler, APIServer


class MockSocket:
    """Mock socket providing bytes to BaseHTTPRequestHandler."""

    def __init__(self, request_bytes: bytes):
        self.rfile = io.BytesIO(request_bytes)
        self.wfile = io.BytesIO()

    def sendall(self, data: bytes):
        self.wfile.write(data)

    def makefile(self, mode, *args, **kwargs):
        if "b" in mode:
            if "r" in mode:
                return self.rfile
            elif "w" in mode:
                return self.wfile
        return self.rfile


def run_handler(method: str, path: str, headers: dict = None, body: dict = None, secret: str = "test_secret"):
    """Run an APIHandler request in memory and parse response."""
    mock_engine = MagicMock()
    mock_engine.get_status.return_value = {
        "status": "ready",
        "version": "1.0.0",
        "ollama_available": False,
        "sandbox_available": False,
    }
    mock_engine.list_scans.return_value = []
    mock_engine.get_config.return_value = {"api_port": 18329}

    headers = headers or {}
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    if body_bytes:
        headers["Content-Length"] = str(len(body_bytes))
        headers["Content-Type"] = "application/json"

    raw_req = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
    for k, v in headers.items():
        raw_req += f"{k}: {v}\r\n"
    raw_req += "\r\n"
    raw_bytes = raw_req.encode("utf-8") + body_bytes

    sock = MockSocket(raw_bytes)

    # Subclass handler
    handler_class = type(
        "TestHandler",
        (APIHandler,),
        {"engine": mock_engine, "api_secret": secret},
    )

    # Initialize handler
    client_address = ("127.0.0.1", 12345)
    server = MagicMock()
    handler = handler_class(sock, client_address, server)

    # Extract written response
    sock.wfile.seek(0)
    response_data = sock.wfile.read()
    
    # Parse status and body
    status_line = response_data.split(b"\r\n", 1)[0].decode("utf-8")
    status_code = int(status_line.split()[1])
    
    # Body is after \r\n\r\n
    parts = response_data.split(b"\r\n\r\n", 1)
    resp_body = {}
    if len(parts) > 1 and parts[1]:
        try:
            resp_body = json.loads(parts[1].decode("utf-8"))
        except Exception:
            resp_body = parts[1].decode("utf-8")

    return status_code, resp_body, response_data.decode("utf-8", errors="replace")


class TestAPIHandlerInMemory(unittest.TestCase):
    """Test API authentication, CORS, and routing purely in memory."""

    def test_unauthorized_when_secret_invalid(self):
        status, body, _ = run_handler("GET", "/api/status", headers={"X-RepoGuard-Secret": "wrong"})
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_unauthorized_when_secret_missing(self):
        status, body, _ = run_handler("GET", "/api/status", headers={})
        self.assertEqual(status, 401)

    def test_authorized_status(self):
        status, body, _ = run_handler("GET", "/api/status", headers={"X-RepoGuard-Secret": "test_secret"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["version"], "1.0.0")

    def test_cors_preflight_options(self):
        status, body, raw = run_handler(
            "OPTIONS", "/api/status",
            headers={"Origin": "http://127.0.0.1", "X-RepoGuard-Secret": "test_secret"}
        )
        self.assertEqual(status, 204)
        self.assertIn("Access-Control-Allow-Origin: http://127.0.0.1", raw)

    def test_clear_requires_confirmation(self):
        status, body, _ = run_handler(
            "POST", "/api/clear",
            headers={"X-RepoGuard-Secret": "test_secret"},
            body={"confirm": False}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_clear_succeeds_with_confirmation(self):
        status, body, _ = run_handler(
            "POST", "/api/clear",
            headers={"X-RepoGuard-Secret": "test_secret"},
            body={"confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "cleared")

    def test_cross_origin_evil_rejected(self):
        """Cross-origin request from evil.example is rejected."""
        status, body, raw = run_handler(
            "OPTIONS", "/api/status",
            headers={"Origin": "http://evil.example", "X-RepoGuard-Secret": "test_secret"}
        )
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", raw)

    def test_cross_origin_localhost_evil_rejected(self):
        """Cross-origin request from localhost.evil.com is rejected."""
        status, body, raw = run_handler(
            "OPTIONS", "/api/status",
            headers={"Origin": "http://localhost.evil.com", "X-RepoGuard-Secret": "test_secret"}
        )
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", raw)

    def test_post_text_plain_rejected(self):
        """POST with Content-Type text/plain must be rejected (415)."""
        mock_engine = MagicMock()
        sock = MockSocket(
            b"POST /api/clear HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: http://evil.example\r\n"
            b"X-RepoGuard-Secret: test_secret\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 17\r\n\r\n"
            b'{"confirm": true}'
        )
        handler_class = type("TestHandler", (APIHandler,), {"engine": mock_engine, "api_secret": "test_secret"})
        handler = handler_class(sock, ("127.0.0.1", 12345), MagicMock())
        sock.wfile.seek(0)
        resp = sock.wfile.read().decode("utf-8")
        status = int(resp.split("\r\n")[0].split()[1])
        # Either 403 (origin not allowed) or 415 (unsupported media type)
        self.assertIn(status, (403, 415))

    def test_host_header_validation(self):
        """Host header not pointing to loopback must be rejected with 400."""
        mock_engine = MagicMock()
        sock = MockSocket(
            b"GET /api/status HTTP/1.1\r\n"
            b"Host: evil.com\r\n"
            b"X-RepoGuard-Secret: test_secret\r\n\r\n"
        )
        handler_class = type("TestHandler", (APIHandler,), {"engine": mock_engine, "api_secret": "test_secret"})
        handler = handler_class(sock, ("127.0.0.1", 12345), MagicMock())
        sock.wfile.seek(0)
        resp = sock.wfile.read().decode("utf-8")
        status = int(resp.split("\r\n")[0].split()[1])
        self.assertEqual(status, 400)

    def test_restricted_config_keys_rejected(self):
        """Arbitrary endpoints or images in config cannot be updated via /api/config."""
        status, body, _ = run_handler(
            "POST", "/api/config",
            headers={"X-RepoGuard-Secret": "test_secret"},
            body={"ollama_endpoint": "http://evil.com/api", "sandbox_image": "evil:latest"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("restricted", body["error"])

    def test_secret_generated_on_first_run_and_reused_on_second(self):
        import tempfile
        import os
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with unittest.mock.patch("pathlib.Path.home", return_value=fake_home):
                mock_engine = MagicMock()
                server1 = APIServer(engine=mock_engine)
                secret1 = server1.secret
                self.assertTrue(secret1)

                secret_file = fake_home / ".repoguard" / "api_secret"
                self.assertTrue(secret_file.is_file())
                self.assertEqual(secret_file.read_text().strip(), secret1)
                file_mode = oct(os.stat(secret_file).st_mode & 0o777)
                self.assertEqual(file_mode, oct(0o600))

                server2 = APIServer(engine=mock_engine)
                self.assertEqual(server2.secret, secret1)

    def test_explicit_secret_overrides_and_writes_nothing(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with unittest.mock.patch("pathlib.Path.home", return_value=fake_home):
                mock_engine = MagicMock()
                server = APIServer(engine=mock_engine, secret="override_secret_123")
                self.assertEqual(server.secret, "override_secret_123")

                secret_file = fake_home / ".repoguard" / "api_secret"
                self.assertFalse(secret_file.exists())


if __name__ == "__main__":
    unittest.main()
