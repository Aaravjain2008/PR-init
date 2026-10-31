#!/usr/bin/env python3
"""Chrome Native Messaging Host for RepoGuard.

Communicates with the Chrome extension background script via standard input/output.
Protocol: 4-byte native endian integer indicating message length, followed by JSON.

Security model:
- Accepts only a narrow, documented, validated set of message types:
  'get_status', 'scan_item', 'scan_repository', 'get_scan', 'list_scans'
- Rejects any unrecognized action or payload
- Never executes arbitrary shell commands
- Input length is strictly capped to 1MB
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
from pathlib import Path
from typing import Any, Dict

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from repoguard.engine import RepoGuardEngine
from repoguard.models import EngineConfig

# Set up logging to file (never to stdout, as stdout is the protocol channel)
log_path = Path.home() / ".repoguard_native.log"
logging.basicConfig(
    filename=str(log_path),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("repoguard.native_host")


def read_message() -> Optional[Dict[str, Any]]:
    """Read a length-prefixed message from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return None
    message_length = struct.unpack("@I", raw_length)[0]
    if message_length > 1024 * 1024:  # 1MB limit
        logger.error(f"Message exceeds size limit: {message_length} bytes")
        return None

    raw_data = sys.stdin.buffer.read(message_length)
    if len(raw_data) < message_length:
        return None

    try:
        return json.loads(raw_data.decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to decode message: {e}")
        return None


def send_message(message: Dict[str, Any]) -> None:
    """Send a length-prefixed JSON message to stdout."""
    encoded = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def handle_message(msg: Dict[str, Any], engine: RepoGuardEngine) -> Dict[str, Any]:
    """Validate and dispatch allowed message actions."""
    action = msg.get("action")
    
    if action == "get_status":
        return engine.get_status()

    elif action == "scan_item":
        repo = msg.get("repository")
        item_type = msg.get("item_type", "issue")
        number = msg.get("number")
        if not repo or not number:
            return {"error": "Missing repository or number"}
        scan = engine.scan_item(repo, item_type, int(number))
        return scan.to_dict()

    elif action == "scan_repository":
        repo = msg.get("repository")
        item_type = msg.get("item_type")
        state = msg.get("state", "open")
        if not repo:
            return {"error": "Missing repository"}
        scan = engine.scan_repository(repo, item_type=item_type, state=state)
        return scan.to_dict()

    elif action == "get_scan":
        scan_id = msg.get("scan_id")
        if not scan_id:
            return {"error": "Missing scan_id"}
        scan = engine.get_scan(scan_id)
        return scan or {"error": "Scan not found"}

    elif action == "list_scans":
        repo = msg.get("repository")
        limit = int(msg.get("limit", 20))
        return {"scans": engine.list_scans(repository=repo, limit=limit)}

    logger.warning(f"Rejected unauthorized action: {action}")
    return {"error": f"Invalid action: {action}"}


def main():
    """Main loop for native messaging host."""
    logger.info("RepoGuard Native Host starting up...")
    config = EngineConfig()
    engine = RepoGuardEngine(config)

    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            response = handle_message(msg, engine)
            send_message(response)
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            send_message({"error": str(e)})


if __name__ == "__main__":
    main()

