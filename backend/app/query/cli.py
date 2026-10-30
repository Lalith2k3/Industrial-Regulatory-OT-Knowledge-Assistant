"""Command line entry point: `python -m app.query <command>` (run from backend/)."""

import argparse
import io
import json
import sys
from collections import defaultdict
from collections.abc import Callable

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.ingestion.domain.models import DocType, Domain
from app.query import bootstrap
from app.query.domain.errors import QueryError
from app.query.domain.models import Answer, QueryRequest, RetrievedChunk, SearchFilters
from app.query.evaluation import QuestionResult, RetrievalEvaluator, load_evaluation_set, summarize
from app.query.storage import build_query_schema, create_query_tables

EXIT_OK, EXIT_ERROR = 0, 2
RECALL_AT = (1, 3, 5, 10)


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # citations contain "§" and "–"
    args = _parser().parse_args(argv)
    configure_logging(json_logs=args.json_logs)
    settings = get_settings()
    command: Callable[[Settings, argparse.Namespace], int] = args.handler
    try:
        return command(settings, args)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.query", description="Ask the knowledge base")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON log lines instead of console output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the query log and conversation tables").set_defaults(handler=_init_db)

    ask = sub.add_parser("ask", help="answer a question with citations")
    ask.add_argument("question")
    _add_filters(ask)
    ask.add_argument("--json", action="store_true", help="print the full answer as JSON")
    ask.set_defaults(handler=_ask)

    search = sub.add_parser("search", help="show what retrieval returns for a question (no LLM)")
    search.add_argument("question")
    _add_filters(search)
    search.set_defaults(handler=_search)

    evaluate = sub.add_parser("evaluate-retrieval", help="measure retrieval against the gold question set")
    evaluate.add_argument("--show-hits", action="store_true", help="list every question, not only misses")
    evaluate.set_defaults(handler=_evaluate)
    return parser


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--domain", action="append", choices=[d.value for d in Domain], dest="domains")
    parser.add_argument("--doc-type", action="append", choices=[t.value for t in DocType], dest="doc_types")
    parser.add_argument("--publisher", action="append", dest="publishers")
    parser.add_argument("--document", action="append", dest="document_ids", help="source id from sources.yaml")


def _filters(args: argparse.Namespace) -> SearchFilters:
    return SearchFilters(
        domains=args.domains or [],
        doc_types=args.doc_types or [],
        publishers=args.publishers or [],
        document_ids=args.document_ids or [],
    )


def _init_db(settings: Settings, args: argparse.Namespace) -> int:
    create_query_tables(bootstrap.build_engine(settings), build_query_schema())
    print("query log and conversation tables are ready")
    return EXIT_OK


def _ask(settings: Settings, args: argparse.Namespace) -> int:
    answer = bootstrap.build_rag_service(settings).answer(QueryRequest(question=args.question, filters=_filters(args)))
    if args.json:
        print(json.dumps(answer.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        _print_answer(answer)
    return EXIT_OK


def _search(settings: Settings, args: argparse.Namespace) -> int:
    searcher = bootstrap.build_searcher(settings, bootstrap.build_engine(settings))
    outcome = searcher.search(args.question, _filters(args))
    print(f"references: cfr={outcome.query.cfr_refs} controls={outcome.query.control_refs}")
    print(f"embedding {outcome.embedding_ms} ms, retrieval {outcome.retrieval_ms} ms\n")
    _print_retrieved(outcome.chunks)
    return EXIT_OK


def _evaluate(settings: Settings, args: argparse.Namespace) -> int:
    questions = load_evaluation_set(settings.evaluation_file)
    searcher = bootstrap.build_searcher(settings, bootstrap.build_engine(settings))
    results = RetrievalEvaluator(searcher, bootstrap.build_rewriter(settings)).evaluate(questions)

    ks = [k for k in RECALL_AT if k <= settings.retrieval_top_k]
    by_category: dict[str, list[QuestionResult]] = defaultdict(list)
    for r in results:
        by_category[r.question.category].append(r)
    print(f"{'category':22} {'n':>3} " + " ".join(f"{'R@' + str(k):>6}" for k in ks) + f" {'MRR':>6}")
    for category, group in [*sorted(by_category.items()), ("ALL", results)]:
        s = summarize(group, ks)
        print(f"{category:22} {s.questions:3} " + " ".join(f"{s.recall[k]:6.2f}" for k in ks) + f" {s.mrr:6.2f}")

    print()
    for r in results:
        missed = [c for c, rank in r.ranks.items() if rank is None]
        if missed or args.show_hits:
            found = ", ".join(f"{c} @{rank}" for c, rank in r.ranks.items() if rank is not None)
            print(f"{r.question.id} [{r.question.category}] found: {found or '-'}; missed: {', '.join(missed) or '-'}")
            if r.question.conversation:
                print(f"    searched as: {r.search_text}")
    return EXIT_OK


def _print_answer(answer: Answer) -> None:
    print(answer.text)
    print()
    for c in answer.citations:
        pages = f", p. {c.page_label_start}" if c.page_label_start else ""
        print(f"[{c.number}] {c.citation}{pages} ({c.document_version})")
        if c.url:
            print(f"    {c.url}")
    t = answer.timings
    print(
        f"\nstatus: {answer.status.value} | rewrite {t.rewrite_ms} ms, embedding {t.embedding_ms} ms, "
        f"retrieval {t.retrieval_ms} ms, generation {t.generation_ms} ms, total {t.total_ms} ms"
    )
    if answer.standalone_question != answer.question:
        print(f"searched as: {answer.standalone_question}")


def _print_retrieved(chunks: list[RetrievedChunk]) -> None:
    for i, r in enumerate(chunks, 1):
        ranks = " ".join(f"{name}#{rank}" for name, rank in sorted(r.ranks.items()))
        print(f"{i:2}. {r.score:.4f}  {r.chunk.citation}  [{ranks}]")
        print(f"    {r.chunk.heading[:110]}")
