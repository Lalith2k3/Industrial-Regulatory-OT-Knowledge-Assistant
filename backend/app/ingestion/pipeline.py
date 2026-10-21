"""Ingestion orchestration: registry -> parse -> chunk -> quality gate -> embed -> store.

Each document is processed independently; a failure is recorded and the run continues, and the
previously stored version of that document stays live (NFR-04). Re-running is idempotent:
unchanged documents are skipped and unchanged chunks keep their embeddings.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.core.config import Settings
from app.ingestion import quality
from app.ingestion.chunking import CHUNKER_VERSION, POLICIES, chunk_document
from app.ingestion.embeddings import Embedder, build_embedder
from app.ingestion.errors import IngestionError, RegistryError
from app.ingestion.models import Chunk, ParsedDocument, SourceSpec
from app.ingestion.parsers import get_parser
from app.ingestion.registry import load_registry
from app.ingestion.store import KnowledgeStore

log = structlog.get_logger("ingestion")


@dataclass
class RunOptions:
    source_ids: list[str] | None = None
    force: bool = False  # re-process even if nothing changed
    dry_run: bool = False  # parse, chunk and check only; no embeddings, no database
    prune: bool = True  # remove documents that are no longer enabled in the registry


@dataclass
class DocumentResult:
    source_id: str
    status: str  # ingested | unchanged | checked | failed | pruned
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    seconds: float = 0.0
    error: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)


def run_ingestion(
    settings: Settings,
    options: RunOptions,
    *,
    store: KnowledgeStore | None = None,
    embedder: Embedder | None = None,
) -> list[DocumentResult]:
    specs = load_registry(settings.sources_file)
    known = {s.id for s in specs}
    if options.source_ids and (unknown := set(options.source_ids) - known):
        raise RegistryError(f"unknown source ids: {sorted(unknown)}")
    selected = [s for s in specs if s.enabled and (not options.source_ids or s.id in options.source_ids)]
    if options.source_ids and (disabled := set(options.source_ids) - {s.id for s in selected}):
        raise RegistryError(f"sources are disabled in the registry: {sorted(disabled)}")

    run_id = None
    if not options.dry_run:
        store = store or KnowledgeStore(settings.database_url, settings.embedding_dim)
        store.init_schema()
        embedder = embedder or build_embedder(settings)
        if embedder.dim != settings.embedding_dim:
            raise IngestionError(f"embedder dimension {embedder.dim} != RAG_EMBEDDING_DIM {settings.embedding_dim}")
        run_id = store.start_run(asdict(options))

    results: list[DocumentResult] = []
    try:
        for spec in selected:
            results.append(_process(spec, settings, options, store, embedder))
        if options.prune and not options.dry_run and not options.source_ids:
            enabled = {s.id for s in specs if s.enabled}
            for doc_id in store.document_ids():
                if doc_id not in enabled:
                    store.delete_document(doc_id)
                    log.info("document.pruned", source=doc_id)
                    results.append(DocumentResult(source_id=doc_id, status="pruned"))
    except BaseException as exc:
        if run_id:
            store.finish_run(run_id, "aborted", _summary(results), error=repr(exc))
        raise

    if run_id:
        failed = sum(r.status == "failed" for r in results)
        status = "succeeded" if not failed else ("failed" if failed == len(selected) else "partial")
        store.finish_run(run_id, status, _summary(results))
    return results


def _process(
    spec: SourceSpec,
    settings: Settings,
    options: RunOptions,
    store: KnowledgeStore | None,
    embedder: Embedder | None,
) -> DocumentResult:
    started = time.perf_counter()
    bound = log.bind(source=spec.id)
    try:
        path = settings.data_dir / spec.path
        if not path.is_file():
            hint = " (run `python -m app.ingestion fetch`)" if spec.fetch else ""
            raise IngestionError(f"source file not found: {path}{hint}")
        content_sha = _sha256(path)
        spec_sha = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
        parser = get_parser(spec.parser)

        if store is not None and embedder is not None and not options.force:
            state = store.document_state(spec.id)
            if state and (
                state["content_sha256"],
                state["parser_version"],
                state["chunker_version"],
                state["embedding_model"],
                state["metadata"].get("spec_sha256"),
            ) == (content_sha, parser.version, CHUNKER_VERSION, embedder.model, spec_sha):
                bound.info("document.unchanged")
                return DocumentResult(spec.id, "unchanged", chunks=state["chunk_count"], seconds=_since(started))

        parsed = parser.parse(spec, path)
        policy = POLICIES[spec.doc_type]
        chunks = chunk_document(spec, parsed, policy)
        report = quality.check(spec, parsed, chunks, policy)
        _write_artifacts(settings.processed_dir / spec.id, parsed, chunks, report)
        bound.info("document.parsed", sections=len(parsed.sections), chunks=len(chunks), warnings=len(report.warnings))
        for warning in report.warnings:
            bound.warning("document.quality_warning", detail=warning)
        if not report.ok:
            raise IngestionError("quality gate failed: " + "; ".join(report.errors))

        if options.dry_run or store is None or embedder is None:
            return DocumentResult(spec.id, "checked", chunks=len(chunks), seconds=_since(started), quality=report.stats)

        previous = store.reusable_embeddings(spec.id, embedder.model)
        embeddings = {c.id: previous[c.id][1] for c in chunks if c.id in previous and previous[c.id][0] == c.content_sha256}
        todo = [c for c in chunks if c.id not in embeddings]
        if todo:
            t = time.perf_counter()
            vectors = embedder.embed_documents([c.embed_text for c in todo])
            embeddings.update({c.id: v for c, v in zip(todo, vectors, strict=True)})
            bound.info("document.embedded", chunks=len(todo), seconds=_since(t))

        store.replace_document(
            spec,
            local_path=spec.path.as_posix(),
            content_sha256=content_sha,
            parser=parser.name,
            parser_version=parser.version,
            chunker_version=CHUNKER_VERSION,
            embedding_model=embedder.model,
            chunks=chunks,
            embeddings=embeddings,
            metadata={
                **parsed.metadata,
                "dictionaries": parsed.dictionaries,
                "quality": report.stats,
                "spec_sha256": spec_sha,
            },
        )
        result = DocumentResult(
            spec.id,
            "ingested",
            chunks=len(chunks),
            embedded=len(todo),
            reused=len(chunks) - len(todo),
            seconds=_since(started),
            quality=report.stats,
        )
        bound.info("document.ingested", chunks=result.chunks, embedded=result.embedded, reused=result.reused, seconds=result.seconds)
        return result
    except Exception as exc:  # one bad document must not abort the run
        bound.error("document.failed", error=str(exc), exc_info=not isinstance(exc, IngestionError))
        return DocumentResult(spec.id, "failed", seconds=_since(started), error=str(exc))


def _write_artifacts(out_dir: Path, parsed: ParsedDocument, chunks: list[Chunk], report: quality.QualityReport) -> None:
    """Persist intermediate output so a poor answer can be traced back to parsing or chunking (US-08)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sections.json").write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    with (out_dir / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(chunk.model_dump_json() + "\n")
    (out_dir / "quality.json").write_text(
        json.dumps({"errors": report.errors, "warnings": report.warnings, "stats": report.stats}, indent=2),
        encoding="utf-8",
    )
    for name, table in parsed.dictionaries.items():
        (out_dir / f"{name}.json").write_text(json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _since(started: float) -> float:
    return round(time.perf_counter() - started, 2)


def _summary(results: list[DocumentResult]) -> dict[str, Any]:
    return {"documents": [asdict(r) for r in results]}
