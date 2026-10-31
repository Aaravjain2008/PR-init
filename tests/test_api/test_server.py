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


if __name__ == "__main__":
    unittest.main()
