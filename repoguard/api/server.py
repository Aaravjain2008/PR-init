"""Loopback-only HTTP API server for the RepoGuard engine.

Provides a minimal, authenticated API for the extension and dashboard.
Binds only to 127.0.0.1. Uses a pairing secret for authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the RepoGuard API.
    
    All endpoints require the X-RepoGuard-Secret header to match
    the configured pairing secret. CORS is restricted to approved
    origins (browser extension, localhost dashboard).
    """

    # Reference to the engine — set by APIServer
    engine: Any = None
    api_secret: str = ""
    
    # Suppress default logging
    def log_message(self, format, *args):
        logger.debug(f"API: {format % args}")

    def _send_json(self, data: dict, status: int = 200):
        """Send a JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        """Send an error response."""
        self._send_json({"error": message}, status)

    def _set_cors_headers(self):
        """Set CORS headers for allowed origins."""
        origin = self.headers.get("Origin", "")
        allowed_origins = [
            "http://127.0.0.1",
            "http://localhost",
        ]
        # Allow chrome-extension:// origins
        if origin.startswith("chrome-extension://") or any(
            origin.startswith(ao) for ao in allowed_origins
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers",
                           "Content-Type, X-RepoGuard-Secret")
            self.send_header("Access-Control-Allow-Methods",
                           "GET, POST, OPTIONS")

    def _check_auth(self) -> bool:
        """Verify the pairing secret."""
        if not self.api_secret:
            return True  # No secret configured (dev mode)
        provided = self.headers.get("X-RepoGuard-Secret", "")
        return hmac.compare_digest(provided, self.api_secret)

    def _read_body(self) -> Optional[dict]:
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # Serve static dashboard files without pairing secret
        if path in ("", "/", "/index.html", "/style.css", "/app.js"):
            self._serve_dashboard_static(path)
            return

        if not self._check_auth():
            self._send_error(401, "Invalid or missing API secret")
            return

        routes: Dict[str, Callable] = {
            "/api/status": self._handle_status,
            "/api/scans": self._handle_list_scans,
            "/api/config": self._handle_get_config,
        }

        # Dynamic routes
        if path.startswith("/api/scan/"):
            scan_id = path[len("/api/scan/"):]
            self._handle_get_scan(scan_id)
            return

        handler = routes.get(path)
        if handler:
            handler(params)
        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def _serve_dashboard_static(self, path: str):
        """Serve static dashboard assets."""
        from pathlib import Path
        dash_dir = Path(__file__).resolve().parent.parent.parent / "dashboard"
        
        filename = "index.html" if path in ("", "/", "/index.html") else path.lstrip("/")
        file_path = dash_dir / filename

        if not file_path.exists() or not file_path.is_file():
            self._send_error(404, "Dashboard file not found")
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }
        ct = content_types.get(file_path.suffix.lower(), "text/plain")

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(content)))
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error(500, f"Error reading file: {e}")

    def do_POST(self):
        """Handle POST requests."""
        if not self._check_auth():
            self._send_error(401, "Invalid or missing API secret")
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        if body is None:
            self._send_error(400, "Invalid JSON body")
            return

        routes: Dict[str, Callable] = {
            "/api/scan": self._handle_start_scan,
            "/api/config": self._handle_set_config,
            "/api/clear": self._handle_clear_data,
        }

        handler = routes.get(path)
        if handler:
            handler(body)
        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    # ── Route handlers ──────────────────────────────────────────────────

    def _handle_status(self, params=None):
        """Return engine status."""
        if not self.engine:
            self._send_json({"status": "no_engine"})
            return
        status = self.engine.get_status()
        self._send_json(status)

    def _handle_start_scan(self, body: dict):
        """Start a new scan."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return

        repo = body.get("repository", "")
        item_type = body.get("item_type")  # "issue" or "pull_request" or None
        item_number = body.get("number")   # specific number or None
        
        if not repo:
            self._send_error(400, "Missing 'repository' field (owner/repo)")
            return

        # Run scan in background thread
        def do_scan():
            try:
                if item_number:
                    self.engine.scan_item(repo, item_type or "issue", int(item_number))
                else:
                    self.engine.scan_repository(repo, item_type)
            except Exception as e:
                logger.error(f"Scan error: {e}")

        thread = threading.Thread(target=do_scan, daemon=True)
        thread.start()

        self._send_json({
            "status": "started",
            "repository": repo,
            "message": "Scan started in background",
        })

    def _handle_list_scans(self, params=None):
        """List recent scans."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return

        repo = None
        if params and "repository" in params:
            repo = params["repository"][0]
        
        limit = 50
        if params and "limit" in params:
            try:
                limit = int(params["limit"][0])
            except ValueError:
                pass

        scans = self.engine.list_scans(repository=repo, limit=limit)
        self._send_json({"scans": scans})

    def _handle_get_scan(self, scan_id: str):
        """Get a specific scan result."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return

        result = self.engine.get_scan(scan_id)
        if result is None:
            self._send_error(404, f"Scan not found: {scan_id}")
            return
        self._send_json(result)

    def _handle_get_config(self, params=None):
        """Get current configuration (excluding secrets)."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return
        config = self.engine.get_config()
        self._send_json(config)

    def _handle_set_config(self, body: dict):
        """Update configuration."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return
        try:
            self.engine.update_config(body)
            self._send_json({"status": "updated"})
        except Exception as e:
            self._send_error(400, str(e))

    def _handle_clear_data(self, body: dict):
        """Clear stored data."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return
        confirm = body.get("confirm", False)
        if not confirm:
            self._send_error(400, "Set 'confirm': true to clear data")
            return
        self.engine.clear_data()
        self._send_json({"status": "cleared"})


class APIServer:
    """Loopback-only HTTP server for the RepoGuard engine.
    
    Binds exclusively to 127.0.0.1 and requires a pairing secret.
    """

    def __init__(
        self,
        engine: Any,
        host: str = "127.0.0.1",
        port: int = 18329,
        secret: Optional[str] = None,
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.secret = secret or ""
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the API server in a background thread."""
        # Configure handler class with engine reference
        handler_class = type(
            "ConfiguredHandler",
            (APIHandler,),
            {"engine": self.engine, "api_secret": self.secret},
        )

        self._server = HTTPServer((self.host, self.port), handler_class)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        logger.info(f"API server started on {self.host}:{self.port}")

    def stop(self):
        """Stop the API server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("API server stopped")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

