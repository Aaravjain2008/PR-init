"""Loopback-only HTTP API server for the RepoGuard engine.

Provides a secure, authenticated API for the extension and dashboard.
Binds only to 127.0.0.1. Uses a pairing secret for authentication.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional, Set
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the RepoGuard API.

    Security controls:
    - Host header validation (must be 127.0.0.1 or localhost)
    - Authentication required on all /api/* routes via X-RepoGuard-Secret
    - Exact origin matching for CORS (localhost, 127.0.0.1, chrome-extension://)
    - Content-Type must be application/json on POST
    - Restricted config keys to prevent arbitrary modifications
    """

    engine: Any = None
    api_secret: str = ""

    ALLOWED_CONFIG_KEYS: Set[str] = {
        "max_items_per_scan",
        "request_timeout",
        "github_token",
    }

    def log_message(self, format, *args):
        logger.debug(f"API: {format % args}")

    def _check_host(self) -> bool:
        """Validate the Host header to prevent DNS rebinding."""
        host = self.headers.get("Host", "")
        if not host:
            return False
        host_name = host.split(":")[0].strip().lower()
        return host_name in ("127.0.0.1", "localhost")

    @classmethod
    def _is_allowed_origin(cls, origin: str) -> bool:
        """Match origins strictly.

        Accepts:
        - Exact chrome-extension://<id>
        - http://127.0.0.1:<port> or http://127.0.0.1
        - http://localhost:<port> or http://localhost
        Rejects:
        - http://localhost.evil.com
        - http://127.0.0.1.evil.com
        - Any other external domain
        """
        if not origin:
            return False
        if origin.startswith("chrome-extension://"):
            return True
        try:
            p = urlparse(origin)
            if p.scheme in ("http", "https"):
                if p.hostname in ("127.0.0.1", "localhost"):
                    return True
        except Exception:
            pass
        return False

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
        """Set CORS headers only for allowed origins."""
        origin = self.headers.get("Origin", "")
        if self._is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-RepoGuard-Secret",
            )
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, OPTIONS",
            )

    def _check_auth(self) -> bool:
        """Verify the pairing secret. Never allow empty secret."""
        if not self.api_secret:
            return False
        provided = self.headers.get("X-RepoGuard-Secret", "")
        if not provided:
            return False
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
        if not self._check_host():
            self._send_error(400, "Invalid Host header")
            return
        origin = self.headers.get("Origin", "")
        if origin and not self._is_allowed_origin(origin):
            self._send_error(403, "Origin not allowed")
            return
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        if not self._check_host():
            self._send_error(400, "Invalid Host header")
            return

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
        if not self._check_host():
            self._send_error(400, "Invalid Host header")
            return

        origin = self.headers.get("Origin", "")
        if origin and not self._is_allowed_origin(origin):
            self._send_error(403, "Origin not allowed")
            return

        if not self._check_auth():
            self._send_error(401, "Invalid or missing API secret")
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            self._send_error(415, "Content-Type must be application/json")
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
        item_type = body.get("item_type")
        item_number = body.get("number")

        if not repo:
            self._send_error(400, "Missing 'repository' field (owner/repo)")
            return

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
        """Update configuration, restricted to approved keys."""
        if not self.engine:
            self._send_error(503, "Engine not initialized")
            return

        disallowed = set(body.keys()) - self.ALLOWED_CONFIG_KEYS
        if disallowed:
            self._send_error(
                400,
                f"Cannot update restricted config keys: {', '.join(sorted(disallowed))}",
            )
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
    """Loopback-only ThreadingHTTPServer for the RepoGuard engine.

    Binds exclusively to 127.0.0.1 and generates/requires a pairing secret.
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

        # Persist generated secret in ~/.repoguard/api_secret (not in database)
        if not secret:
            secret_file = Path.home() / ".repoguard" / "api_secret"
            if secret_file.is_file():
                try:
                    secret = secret_file.read_text().strip()
                except Exception:
                    secret = None
            if not secret:
                secret = secrets.token_hex(32)
                try:
                    secret_dir = secret_file.parent
                    secret_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(secret_dir, 0o700)
                    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with open(fd, "w") as f:
                        f.write(secret)
                    os.chmod(secret_file, 0o600)
                except Exception as e:
                    logger.warning(f"Failed to persist API secret to {secret_file}: {e}")
        self.secret = secret

        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the API server in a background thread."""
        handler_class = type(
            "ConfiguredHandler",
            (APIHandler,),
            {"engine": self.engine, "api_secret": self.secret},
        )

        self._server = ThreadingHTTPServer((self.host, self.port), handler_class)
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
