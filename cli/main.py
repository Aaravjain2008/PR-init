"""RepoGuard Command Line Interface.

Unified CLI for scanning repositories, inspecting claim verification results,
managing settings, running diagnostics, starting the local server, and
running the evaluation harness.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from repoguard import __version__
from repoguard.engine import RepoGuardEngine
from repoguard.models import EngineConfig, ScanResult, OverallAssessment, ClaimVerdict
from repoguard.api.server import APIServer

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("repoguard.cli")


def _format_scan_report(scan: ScanResult, detailed: bool = True) -> str:
    """Format a ScanResult for human-readable terminal display."""
    lines = []
    lines.append("=" * 72)
    lines.append(f"RepoGuard Scan Report: {scan.repository}")
    lines.append(f"Scan ID: {scan.scan_id}")
    lines.append(f"Scope:   {scan.scope} | Status: {scan.scan_status.value.upper()}")
    lines.append(f"Started: {scan.started_at} | Items Scanned: {scan.items_scanned}/{scan.items_found}")
    lines.append("=" * 72)

    if scan.errors:
        lines.append("\n[!] Errors:")
        for err in scan.errors:
            lines.append(f"  - {err}")

    if scan.warnings:
        lines.append("\n[!] Warnings:")
        for warn in scan.warnings:
            lines.append(f"  - {warn}")

    lines.append("\n--- Triage Queue ---")
    if not scan.items:
        lines.append("  (No items analyzed)")
        return "\n".join(lines)

    for item in scan.items:
        lines.append(f"\n#{item.number} [{item.item_type.value.upper()}] {item.title}")
        lines.append(f"  Author:     @{item.author}")
        lines.append(f"  Assessment: {item.assessment.value} (Confidence: {item.assessment_confidence.value.upper()})")
        lines.append(f"  Reason:     {item.assessment_reason}")
        if item.ai_summary:
            lines.append(f"  {item.ai_summary}")

        if detailed and item.claims:
            lines.append(f"  Claims ({len(item.claims)} total):")
            for c in item.claims:
                icon = {
                    ClaimVerdict.SUPPORTED: "[SUPPORTED]",
                    ClaimVerdict.CONTRADICTED: "[CONTRADICTED]",
                    ClaimVerdict.REPRODUCED: "[REPRODUCED]",
                    ClaimVerdict.NOT_REPRODUCED: "[NOT REPRODUCED]",
                    ClaimVerdict.UNVERIFIABLE: "[UNVERIFIABLE]",
                    ClaimVerdict.MISSING_EVIDENCE: "[MISSING EVIDENCE]",
                }.get(c.verdict, "[?]")
                lines.append(f"    * {icon} {c.text}")
                lines.append(f"      Reason: {c.reason} (Confidence: {c.confidence.value})")
                if c.evidence:
                    lines.append(f"      Evidence ({len(c.evidence)} items):")
                    for ev in c.evidence:
                        loc = f" in {ev.file_path}" if ev.file_path else ""
                        line_ref = f":L{ev.line_start}" if ev.line_start else ""
                        lines.append(f"        - [{ev.evidence_type.value}] {ev.description}{loc}{line_ref}")

    lines.append("\n" + "=" * 72)
    return "\n".join(lines)


def cmd_scan(args, engine: RepoGuardEngine) -> int:
    """Execute a scan for a repo, issue, or PR."""
    repo = args.repository
    logger.info(f"Starting scan on {repo}...")

    if args.issue:
        scan = engine.scan_item(repo, "issue", args.issue)
    elif args.pr:
        scan = engine.scan_item(repo, "pull_request", args.pr)
    else:
        scan = engine.scan_repository(repo, item_type=args.type, state=args.state)

    if args.json:
        print(scan.to_json())
    else:
        print(_format_scan_report(scan, detailed=not args.concise))

    return 0 if scan.scan_status.value != "error" else 1


def cmd_status(args, engine: RepoGuardEngine) -> int:
    """Show engine status, configuration, and environment connectivity."""
    status = engine.get_status()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0

    print("=" * 50)
    print("RepoGuard Engine Status")
    print("=" * 50)
    print(f"Version:            {status.get('version')}")
    print(f"Status:             {status.get('status')}")
    print(f"Ollama Enabled:     {status.get('ollama_enabled')}")
    print(f"Ollama Available:   {status.get('ollama_available')}")
    print(f"Sandbox Enabled:    {status.get('sandbox_enabled')}")
    print(f"Sandbox Available:  {status.get('sandbox_available')}")

    rate_limit = status.get("api_rate_limit")
    if rate_limit and "resources" in rate_limit:
        core = rate_limit["resources"].get("core", {})
        print(f"GitHub API Limit:   {core.get('remaining', '?')}/{core.get('limit', '?')} remaining")
    else:
        print("GitHub API Limit:   Unauthenticated / Unknown")

    return 0


def cmd_report(args, engine: RepoGuardEngine) -> int:
    """Display a past scan report by ID."""
    scan_dict = engine.get_scan(args.scan_id)
    if not scan_dict:
        logger.error(f"Scan '{args.scan_id}' not found.")
        return 1

    scan = ScanResult.from_dict(scan_dict)
    if args.json:
        print(scan.to_json())
    else:
        print(_format_scan_report(scan, detailed=True))
    return 0


def cmd_export(args, engine: RepoGuardEngine) -> int:
    """Export a scan result to a JSON file."""
    scan_dict = engine.get_scan(args.scan_id)
    if not scan_dict:
        logger.error(f"Scan '{args.scan_id}' not found.")
        return 1

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scan_dict, f, indent=2)
    logger.info(f"Scan result successfully exported to {out_path}")
    return 0


def cmd_scans(args, engine: RepoGuardEngine) -> int:
    """List historical scans stored in SQLite."""
    scans = engine.list_scans(repository=args.repo, limit=args.limit)
    if args.json:
        print(json.dumps(scans, indent=2))
        return 0

    if not scans:
        print("No scans found in database.")
        return 0

    print(f"{'SCAN ID':<38} {'REPOSITORY':<25} {'SCOPE':<15} {'ITEMS':<8} {'STATUS'}")
    print("-" * 95)
    for s in scans:
        print(f"{s['scan_id']:<38} {s['repository']:<25} {s['scope']:<15} {s['items_scanned']:<8} {s['scan_status']}")
    return 0


def cmd_diagnostics(args, engine: RepoGuardEngine) -> int:
    """Run system diagnostics for all RepoGuard dependencies and subsystems."""
    print("=" * 60)
    print("RepoGuard System Diagnostics")
    print("=" * 60)
    print(f"[x] Python Version:        {sys.version.split()[0]}")
    print(f"[x] SQLite Database:       {engine.config.db_path} (Accessible)")

    # Check GitHub Auth
    token_set = bool(engine.config.github_token)
    print(f"[x] GitHub Token Config:   {'Configured' if token_set else 'None (Rate limits: 60/hr)'}")
    rate_info = engine._get_rate_limit_safe()
    if rate_info and "resources" in rate_info:
        core = rate_info["resources"].get("core", {})
        print(f"[x] GitHub API Connection: OK ({core.get('remaining')}/{core.get('limit')} requests left)")
    else:
        print("[!] GitHub API Connection: Limited or Unreachable")

    # Check Sandbox
    sb_avail = engine.sandbox.is_available()
    print(f"[x] Sandboxed Runner:      {'Operational (Docker found)' if sb_avail else 'Fallback Mode (Docker not found)'}")

    # Check Ollama
    ol_avail = engine.ollama.is_available() if engine.config.ollama_enabled else False
    print(f"[x] Ollama AI Assistant:   {'Online (' + engine.config.ollama_model + ')' if ol_avail else 'Offline / Disabled'}")

    print("=" * 60)
    print("Diagnostics complete. System ready for evidence-based triage.")
    return 0


def cmd_serve(args, engine: RepoGuardEngine) -> int:
    """Start the loopback HTTP server."""
    port = args.port or engine.config.api_port
    explicit_secret = args.secret or os.environ.get("REPOGUARD_SECRET", "")
    secret = explicit_secret or engine.config.api_secret or ""
    server = APIServer(
        engine=engine,
        host=args.host,
        port=port,
        secret=secret,
    )
    secret_path = Path.home() / ".repoguard" / "api_secret"
    print(f"Starting RepoGuard Loopback API on http://{args.host}:{port}")
    print(f"Dashboard URL: http://{args.host}:{port}/")
    if explicit_secret:
        print("Using the pairing secret you provided (not stored on disk).")
    else:
        print(f"Pairing secret: {server.secret}")
        print(f"Stored in {secret_path} (paste this secret into the extension popup or dashboard)")
    try:
        server.start()
        # Keep main thread alive
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.stop()
    return 0


def cmd_evaluate(args, engine: RepoGuardEngine) -> int:
    """Run the evaluation harness against labeled issue dataset."""
    from repoguard.evaluation.harness import EvaluationHarness
    dataset_path = args.dataset if args.dataset else None
    harness = EvaluationHarness(engine=engine, dataset_path=dataset_path)
    split = getattr(args, "split", "test")
    metrics = harness.run(split=split)
    if args.json:
        print(json.dumps(metrics.to_dict(), indent=2))
    else:
        print(harness.format_summary(metrics))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="repoguard",
        description="RepoGuard: Evidence-based triage for GitHub issues and pull requests",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=str, default="repoguard.db", help="Path to SQLite database")
    parser.add_argument("--token", type=str, default=None, help="GitHub Personal Access Token")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan a repository, issue, or PR")
    p_scan.add_argument("repository", help="Target repository (owner/repo or full URL)")
    p_scan.add_argument("--issue", type=int, help="Scan a specific issue number")
    p_scan.add_argument("--pr", type=int, help="Scan a specific PR number")
    p_scan.add_argument("--type", choices=["issue", "pull_request"], help="Item type filter")
    p_scan.add_argument("--state", choices=["open", "closed", "all"], default="open", help="State filter")
    p_scan.add_argument("--concise", action="store_true", help="Omit claim-level evidence details")
    p_scan.add_argument("--json", action="store_true", help="Output raw JSON")

    # status
    p_status = subparsers.add_parser("status", help="Show engine status and quotas")
    p_status.add_argument("--json", action="store_true", help="Output raw JSON")

    # report
    p_report = subparsers.add_parser("report", help="Display results of a past scan")
    p_report.add_argument("scan_id", help="Scan ID to display")
    p_report.add_argument("--json", action="store_true", help="Output raw JSON")

    # export
    p_export = subparsers.add_parser("export", help="Export scan results to file")
    p_export.add_argument("scan_id", help="Scan ID to export")
    p_export.add_argument("-o", "--output", required=True, help="Output JSON file path")

    # scans
    p_scans = subparsers.add_parser("scans", help="List past scans")
    p_scans.add_argument("--repo", help="Filter by repository")
    p_scans.add_argument("--limit", type=int, default=20, help="Maximum scans to show")
    p_scans.add_argument("--json", action="store_true", help="Output raw JSON")

    # diagnostics
    subparsers.add_parser("diagnostics", help="Run system diagnostics")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the local HTTP API server")
    p_serve.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=18329, help="Port to bind (default: 18329)")
    p_serve.add_argument("--secret", type=str, help="API pairing secret")

    # evaluate
    p_eval = subparsers.add_parser("evaluate", help="Run evaluation harness on labeled dataset")
    p_eval.add_argument("--dataset", type=str, help="Path to labeled dataset JSON")
    p_eval.add_argument("--split", choices=["test", "dev", "all"], default="test", help="Dataset split (default: test)")
    p_eval.add_argument("--json", action="store_true", help="Output raw JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    token = args.token or os.environ.get("GITHUB_TOKEN")
    config = EngineConfig(
        db_path=args.db,
        github_token=token,
    )
    engine = RepoGuardEngine(config=config)

    commands = {
        "scan": cmd_scan,
        "status": cmd_status,
        "report": cmd_report,
        "export": cmd_export,
        "scans": cmd_scans,
        "diagnostics": cmd_diagnostics,
        "serve": cmd_serve,
        "evaluate": cmd_evaluate,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args, engine)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
