"""Command line entry point: `python -m app.ingestion <command>` (run from backend/)."""

import argparse
import sys
from collections.abc import Callable

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.ingestion import bootstrap
from app.ingestion.domain.errors import IngestionError
from app.ingestion.domain.models import DocumentStatus
from app.ingestion.pipeline import DocumentResult, RunOptions
from app.ingestion.sources.registry import load_registry

EXIT_OK, EXIT_DOCUMENT_FAILED, EXIT_ERROR = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(json_logs=args.json_logs)
    settings = get_settings()
    command: Callable[[Settings, argparse.Namespace], int] = args.handler
    try:
        return command(settings, args)
    except IngestionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion", description="Knowledge base ingestion")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON log lines instead of console output")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download the pinned eCFR snapshots declared in sources.yaml")
    fetch.add_argument("--source", action="append", dest="sources", help="limit to these source ids")
    fetch.set_defaults(handler=_fetch)

    sub.add_parser("check-updates", help="report eCFR sources pinned behind the latest eCFR date").set_defaults(
        handler=_check_updates
    )
    sub.add_parser("init-db", help="create the pgvector extension and knowledge base tables").set_defaults(
        handler=_init_db
    )

    run = sub.add_parser("run", help="ingest approved sources into the knowledge base")
    run.add_argument("--source", action="append", dest="sources", help="limit to these source ids")
    run.add_argument("--force", action="store_true", help="re-process even when nothing changed")
    run.add_argument("--dry-run", action="store_true", help="parse, chunk and run quality gates only")
    run.add_argument("--no-prune", action="store_true", help="keep documents that are no longer in the registry")
    run.set_defaults(handler=_run)
    return parser


def _fetch(settings: Settings, args: argparse.Namespace) -> int:
    client = bootstrap.build_ecfr_client(settings)
    for spec in load_registry(settings.sources_file):
        if spec.fetch and spec.enabled and (not args.sources or spec.id in args.sources):
            path = client.download_part(spec.fetch, settings.data_dir / spec.path)
            print(f"{spec.id}: saved eCFR {spec.fetch.date} snapshot to {path}")
    return EXIT_OK


def _check_updates(settings: Settings, args: argparse.Namespace) -> int:
    client = bootstrap.build_ecfr_client(settings)
    behind = 0
    for spec in load_registry(settings.sources_file):
        if spec.fetch and spec.enabled:
            latest = client.latest_date(spec.fetch.title)
            is_behind = latest > spec.fetch.date
            behind += is_behind
            status = f"NEWER TEXT AVAILABLE ({latest})" if is_behind else "UP TO DATE"
            print(f"{spec.id}: pinned {spec.fetch.date} -> {status}")
    if behind:
        print("Update fetch.date (and version) in sources.yaml, then run `fetch` and `run`.")
    return EXIT_OK


def _init_db(settings: Settings, args: argparse.Namespace) -> int:
    bootstrap.build_document_repository(settings).ensure_schema()
    print("knowledge base schema is ready")
    return EXIT_OK


def _run(settings: Settings, args: argparse.Namespace) -> int:
    options = RunOptions(source_ids=args.sources, force=args.force, dry_run=args.dry_run, prune=not args.no_prune)
    pipeline = bootstrap.build_pipeline(settings, dry_run=options.dry_run)
    results = pipeline.run(load_registry(settings.sources_file), options)
    _print_results(results)
    return EXIT_DOCUMENT_FAILED if any(r.status == DocumentStatus.FAILED for r in results) else EXIT_OK


def _print_results(results: list[DocumentResult]) -> None:
    print()
    print(f"{'source':40} {'status':10} {'chunks':>7} {'embedded':>9} {'reused':>7} {'secs':>7}")
    for r in results:
        print(f"{r.source_id:40} {r.status.value:10} {r.chunks:7} {r.embedded:9} {r.reused:7} {r.seconds:7.1f}")
        if r.error:
            print(f"    error: {r.error}")
