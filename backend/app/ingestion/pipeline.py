"""Ingestion orchestration: select sources -> parse -> chunk -> quality gate -> embed -> store.

The pipeline only coordinates. Every capability it uses (parsers, chunker, embedder, document
repository, run log, artifact sink) is passed in by bootstrap.py, so it can be unit-tested with
in-memory fakes. Each document is processed independently: a failure is recorded, the run
continues, and the previously stored version of that document stays live (NFR-04). Re-running is
idempotent: unchanged documents are skipped and unchanged chunks keep their embeddings.
"""

import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.ingestion.chunking import Chunker
from app.ingestion.domain.errors import ConfigurationError, IngestionError, QualityGateError, RegistryError
from app.ingestion.domain.interfaces import ArtifactSink, DocumentRepository, Embedder, Parser, RunLog
from app.ingestion.domain.models import (
    Chunk,
    DocumentFingerprint,
    DocumentRecord,
    DocumentStatus,
    ParsedDocument,
    RunStatus,
    SourceSpec,
)
from app.ingestion.parsing import ParserRegistry
from app.ingestion.quality import QualityGate, QualityReport

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
    status: DocumentStatus
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    seconds: float = 0.0
    error: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeStore:
    """What a full (non dry) run writes to."""

    documents: DocumentRepository
    runs: RunLog
    embedder: Embedder


class IngestionPipeline:
    def __init__(
        self,
        *,
        data_dir: Path,
        parsers: ParserRegistry,
        chunker: Chunker,
        quality_gate: QualityGate,
        artifacts: ArtifactSink,
        store: KnowledgeStore | None = None,
    ):
        self._data_dir = data_dir
        self._parsers = parsers
        self._chunker = chunker
        self._quality_gate = quality_gate
        self._artifacts = artifacts
        self._store = store

    def run(self, specs: list[SourceSpec], options: RunOptions) -> list[DocumentResult]:
        selected = _select(specs, options)
        for spec in selected:
            self._parsers.validate(spec)  # fail fast on a misconfigured registry entry
        if options.dry_run:
            return [self._process(spec, options, store=None) for spec in selected]

        store = self._store
        if store is None:
            raise ConfigurationError("a full run needs a knowledge store; use --dry-run to only parse and chunk")
        store.documents.ensure_schema()
        run_id = store.runs.start(asdict(options))
        results: list[DocumentResult] = []
        try:
            results.extend(self._process(spec, options, store=store) for spec in selected)
            if options.prune and not options.source_ids:
                results.extend(_prune(specs, store.documents))
        except BaseException as exc:
            store.runs.finish(run_id, RunStatus.ABORTED, _summary(results), error=repr(exc))
            raise
        store.runs.finish(run_id, _run_status(results, len(selected)), _summary(results))
        return results

    # -- one document ----------------------------------------------------------------------

    def _process(self, spec: SourceSpec, options: RunOptions, store: KnowledgeStore | None) -> DocumentResult:
        started = time.perf_counter()
        bound = log.bind(source=spec.id)
        try:
            path = self._source_path(spec)
            parser = self._parsers.get(spec.parser)
            fingerprint = self._fingerprint(spec, path, parser, store.embedder) if store else None

            if store and fingerprint and not options.force:
                stored = store.documents.get(spec.id)
                if stored and stored.fingerprint == fingerprint:
                    bound.info("document.unchanged")
                    return DocumentResult(
                        spec.id, DocumentStatus.UNCHANGED, chunks=stored.chunk_count, seconds=_since(started)
                    )

            parsed, chunks, report = self._prepare(spec, path, parser)
            if store is None or fingerprint is None:
                return DocumentResult(
                    spec.id, DocumentStatus.CHECKED, chunks=len(chunks), seconds=_since(started), quality=report.stats
                )

            embeddings, embedded = self._embed(spec.id, chunks, store)
            record = DocumentRecord(
                source=spec,
                local_path=spec.path.as_posix(),
                parser=parser.name,
                fingerprint=fingerprint,
                metadata={**parsed.metadata, "dictionaries": parsed.dictionaries, "quality": report.stats},
            )
            store.documents.replace(record, chunks, embeddings)
            result = DocumentResult(
                spec.id,
                DocumentStatus.INGESTED,
                chunks=len(chunks),
                embedded=embedded,
                reused=len(chunks) - embedded,
                seconds=_since(started),
                quality=report.stats,
            )
            bound.info(
                "document.ingested",
                chunks=result.chunks,
                embedded=result.embedded,
                reused=result.reused,
                seconds=result.seconds,
            )
            return result
        except Exception as exc:  # one bad document must not abort the run
            bound.error("document.failed", error=str(exc), exc_info=not isinstance(exc, IngestionError))
            return DocumentResult(spec.id, DocumentStatus.FAILED, seconds=_since(started), error=str(exc))

    def _source_path(self, spec: SourceSpec) -> Path:
        path = self._data_dir / spec.path
        if not path.is_file():
            hint = " (run `python -m app.ingestion fetch`)" if spec.fetch else ""
            raise IngestionError(f"source file not found: {path}{hint}")
        return path

    def _fingerprint(self, spec: SourceSpec, path: Path, parser: Parser, embedder: Embedder) -> DocumentFingerprint:
        return DocumentFingerprint(
            content_sha256=_file_sha256(path),
            spec_sha256=hashlib.sha256(spec.model_dump_json().encode()).hexdigest(),
            parser_version=parser.version,
            chunker_version=self._chunker.version,
            embedding_model=embedder.model,
        )

    def _prepare(
        self, spec: SourceSpec, path: Path, parser: Parser
    ) -> tuple[ParsedDocument, list[Chunk], QualityReport]:
        """Parse, chunk, quality-check and keep artifacts. Raises if the quality gate fails."""
        bound = log.bind(source=spec.id)
        parsed = parser.parse(spec, path)
        chunks = self._chunker.chunk(spec, parsed)
        report = self._quality_gate.check(parsed, chunks, self._chunker.policy_for(spec))
        self._artifacts.write(parsed, chunks, report.as_dict())
        bound.info("document.parsed", sections=len(parsed.sections), chunks=len(chunks), warnings=len(report.warnings))
        for warning in report.warnings:
            bound.warning("document.quality_warning", detail=warning)
        if not report.ok:
            raise QualityGateError("quality gate failed: " + "; ".join(report.errors))
        return parsed, chunks, report

    def _embed(
        self, document_id: str, chunks: list[Chunk], store: KnowledgeStore
    ) -> tuple[dict[str, list[float]], int]:
        """Reuse stored vectors for unchanged chunks; embed the rest. Returns (vectors, newly embedded)."""
        previous = store.documents.reusable_embeddings(document_id, store.embedder.model)
        vectors = {
            c.id: previous[c.id][1] for c in chunks if c.id in previous and previous[c.id][0] == c.content_sha256
        }
        todo = [c for c in chunks if c.id not in vectors]
        if todo:
            started = time.perf_counter()
            new = store.embedder.embed_documents([c.embed_text for c in todo])
            vectors.update({c.id: v for c, v in zip(todo, new, strict=True)})
            log.info("document.embedded", source=document_id, chunks=len(todo), seconds=_since(started))
        return vectors, len(todo)


def _select(specs: list[SourceSpec], options: RunOptions) -> list[SourceSpec]:
    if not options.source_ids:
        return [s for s in specs if s.enabled]
    requested = set(options.source_ids)
    if unknown := requested - {s.id for s in specs}:
        raise RegistryError(f"unknown source ids: {sorted(unknown)}")
    if disabled := requested - {s.id for s in specs if s.enabled}:
        raise RegistryError(f"sources are disabled in the registry: {sorted(disabled)}")
    return [s for s in specs if s.id in requested]


def _prune(specs: list[SourceSpec], documents: DocumentRepository) -> list[DocumentResult]:
    """Remove stored documents that are no longer enabled in the registry (US-07)."""
    enabled = {s.id for s in specs if s.enabled}
    pruned: list[DocumentResult] = []
    for doc_id in documents.document_ids():
        if doc_id not in enabled:
            documents.delete(doc_id)
            log.info("document.pruned", source=doc_id)
            pruned.append(DocumentResult(doc_id, DocumentStatus.PRUNED))
    return pruned


def _run_status(results: list[DocumentResult], selected: int) -> RunStatus:
    failed = sum(r.status == DocumentStatus.FAILED for r in results)
    if not failed:
        return RunStatus.SUCCEEDED
    return RunStatus.FAILED if failed == selected else RunStatus.PARTIAL


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _since(started: float) -> float:
    return round(time.perf_counter() - started, 2)


def _summary(results: list[DocumentResult]) -> dict[str, Any]:
    return {"documents": [{**asdict(r), "status": r.status.value} for r in results]}
