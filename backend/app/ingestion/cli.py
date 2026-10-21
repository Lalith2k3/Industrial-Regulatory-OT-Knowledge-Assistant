"""Command line entry point: `python -m app.ingestion <command>` (run from backend/)."""

import argparse
import logging
import sys

import structlog

from app.core.config import get_settings
from app.ingestion.errors import IngestionError
from app.ingestion.fetch import fetch_ecfr, latest_ecfr_date
from app.ingestion.pipeline import RunOptions, run_ingestion
from app.ingestion.registry import load_registry
from app.ingestion.store import KnowledgeStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion", description="Knowledge base ingestion")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON log lines instead of console output")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download the pinned eCFR snapshots declared in sources.yaml")
    fetch.add_argument("--source", action="append", dest="sources", help="limit to these source ids")

    sub.add_parser("check-updates", help="report eCFR sources whose pinned date is behind the latest eCFR date")
    sub.add_parser("init-db", help="create the pgvector extension and knowledge base tables")

    run = sub.add_parser("run", help="ingest approved sources into the knowledge base")
    run.add_argument("--source", action="append", dest="sources", help="limit to these source ids")
    run.add_argument("--force", action="store_true", help="re-process even when nothing changed")
    run.add_argument("--dry-run", action="store_true", help="parse, chunk and run quality gates only")
    run.add_argument("--no-prune", action="store_true", help="keep documents that are no longer in the registry")

    args = parser.parse_args(argv)
    _configure_logging(args.json_logs)
    settings = get_settings()
    try:
        if args.command == "fetch":
            for spec in load_registry(settings.sources_file):
                if spec.fetch and spec.enabled and (not args.sources or spec.id in args.sources):
                    path = fetch_ecfr(spec, settings)
                    print(f"{spec.id}: saved eCFR {spec.fetch.date} snapshot to {path}")
            return 0

        if args.command == "check-updates":
            behind = 0
            for spec in load_registry(settings.sources_file):
                if spec.fetch and spec.enabled:
                    latest = latest_ecfr_date(spec.fetch.title, settings)
                    flag = "UP TO DATE" if latest <= spec.fetch.date else f"NEWER TEXT AVAILABLE ({latest})"
                    behind += latest > spec.fetch.date
                    print(f"{spec.id}: pinned {spec.fetch.date} -> {flag}")
            if behind:
                print("Update fetch.date (and version) in sources.yaml, then run `fetch` and `run`.")
            return 0

        if args.command == "init-db":
            KnowledgeStore(settings.database_url, settings.embedding_dim).init_schema()
            print("knowledge base schema is ready")
            return 0

        options = RunOptions(
            source_ids=args.sources, force=args.force, dry_run=args.dry_run, prune=not args.no_prune
        )
        results = run_ingestion(settings, options)
        print()
        print(f"{'source':40} {'status':10} {'chunks':>7} {'embedded':>9} {'reused':>7} {'secs':>7}")
        for r in results:
            print(f"{r.source_id:40} {r.status:10} {r.chunks:7} {r.embedded:9} {r.reused:7} {r.seconds:7.1f}")
            if r.error:
                print(f"    error: {r.error}")
        return 1 if any(r.status == "failed" for r in results) else 0
    except IngestionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _configure_logging(json_logs: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The SDK logs every refused request verbosely; the embedder logs its own short retry message.
    logging.getLogger("voyageai").setLevel(logging.CRITICAL)
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ]
    )
