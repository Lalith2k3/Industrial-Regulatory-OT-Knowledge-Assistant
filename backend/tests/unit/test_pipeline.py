"""Pipeline orchestration with in-memory fakes: no PDF, database or embedding provider involved."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.ingestion.chunking import DEFAULT_POLICIES
from app.ingestion.domain.errors import ConfigurationError, ParseError, RegistryError
from app.ingestion.domain.models import (
    Chunk,
    DocType,
    DocumentRecord,
    DocumentStatus,
    Domain,
    Paragraph,
    ParsedDocument,
    RunStatus,
    Section,
    SectionKind,
    SourceSpec,
    StoredDocument,
)
from app.ingestion.embedding import HashEmbedder
from app.ingestion.parsing import BaseParser, ParserOptions, ParserRegistry
from app.ingestion.pipeline import IngestionPipeline, KnowledgeStore, RunOptions
from app.ingestion.quality import QualityGate


class LineOptions(ParserOptions):
    fail: bool = False


class LineParser(BaseParser):
    """Every non-empty line of a text file becomes one section."""

    name: ClassVar[str] = "lines"
    version: ClassVar[str] = "1"
    Options = LineOptions

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        if self._options(source).fail:
            raise ParseError("simulated parser failure")
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        sections = [
            Section(
                key=f"s{i}", kind=SectionKind.SECTION, heading=f"Section {i}", path=[f"Section {i}"],
                citation=f"{source.short_title} s{i}", paragraphs=[Paragraph(text=line)], metadata={"unit": f"s{i}"},
            )
            for i, line in enumerate(lines)
        ]  # fmt: skip
        return ParsedDocument(source_id=source.id, parser=self.name, parser_version=self.version, sections=sections)


class InMemoryDocuments:
    def __init__(self):
        self.docs: dict[str, tuple[DocumentRecord, list[Chunk], dict[str, list[float]]]] = {}

    def ensure_schema(self) -> None:
        pass

    def get(self, document_id: str) -> StoredDocument | None:
        if document_id not in self.docs:
            return None
        record, chunks, _ = self.docs[document_id]
        return StoredDocument(record.fingerprint, len(chunks))

    def document_ids(self) -> list[str]:
        return list(self.docs)

    def reusable_embeddings(self, document_id: str, model: str) -> dict[str, tuple[str, list[float]]]:
        if document_id not in self.docs:
            return {}
        _, chunks, vectors = self.docs[document_id]
        return {c.id: (c.content_sha256, vectors[c.id]) for c in chunks}

    def replace(self, record: DocumentRecord, chunks: Sequence[Chunk], embeddings: dict[str, list[float]]) -> None:
        self.docs[record.source.id] = (record, list(chunks), dict(embeddings))

    def delete(self, document_id: str) -> None:
        self.docs.pop(document_id, None)


class InMemoryRunLog:
    def __init__(self):
        self.finished: list[RunStatus] = []

    def start(self, options: dict[str, Any]) -> str:
        return "run-1"

    def finish(self, run_id: str, status: RunStatus, report: dict[str, Any], error: str | None = None) -> None:
        self.finished.append(status)


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__(dim=8)
        self.embedded = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return super().embed_documents(texts)


class RecordingArtifacts:
    def __init__(self):
        self.written: list[str] = []

    def write(self, parsed: ParsedDocument, chunks: Sequence[Chunk], quality: dict[str, Any]) -> None:
        self.written.append(parsed.source_id)


def _spec(source_id: str, **overrides: Any) -> SourceSpec:
    fields: dict[str, Any] = {
        "id": source_id, "title": f"Title {source_id}", "short_title": source_id.upper(), "publisher": "PHMSA",
        "domain": Domain.PIPELINE_SAFETY, "doc_type": DocType.REGULATION, "parser": "lines",
        "path": Path(f"{source_id}.txt"), "version": "1",
    }  # fmt: skip
    return SourceSpec(**{**fields, **overrides})


@pytest.fixture
def env(tmp_path, make_chunker):
    for name, text in {"a": "alpha one\nalpha two", "b": "beta one", "c": "gamma one"}.items():
        (tmp_path / f"{name}.txt").write_text(text, encoding="utf-8")
    documents, runs, embedder, artifacts = (
        InMemoryDocuments(),
        InMemoryRunLog(),
        CountingEmbedder(),
        RecordingArtifacts(),
    )

    def pipeline(with_store: bool = True) -> IngestionPipeline:
        return IngestionPipeline(
            data_dir=tmp_path,
            parsers=ParserRegistry([LineParser()]),
            chunker=make_chunker(DEFAULT_POLICIES[DocType.REGULATION]),
            quality_gate=QualityGate(),
            artifacts=artifacts,
            store=KnowledgeStore(documents, runs, embedder) if with_store else None,
        )

    return pipeline, documents, runs, embedder, artifacts, tmp_path


def test_unchanged_documents_are_skipped_and_force_reprocesses_without_re_embedding(env):
    pipeline, _, _, embedder, _, _ = env
    specs = [_spec("a"), _spec("b")]
    assert [r.status for r in pipeline().run(specs, RunOptions())] == [DocumentStatus.INGESTED] * 2
    assert embedder.embedded == 3

    assert [r.status for r in pipeline().run(specs, RunOptions())] == [DocumentStatus.UNCHANGED] * 2
    forced = pipeline().run(specs, RunOptions(force=True))
    assert [r.status for r in forced] == [DocumentStatus.INGESTED] * 2
    assert [r.reused for r in forced] == [2, 1] and embedder.embedded == 3  # stored vectors reused


def test_changing_the_registry_entry_reprocesses_the_document(env):
    pipeline, *_ = env
    pipeline().run([_spec("a")], RunOptions())
    assert pipeline().run([_spec("a", version="2")], RunOptions())[0].status == DocumentStatus.INGESTED


def test_one_failing_document_does_not_stop_the_run(env, tmp_path):
    pipeline, documents, runs, *_ = env
    (tmp_path / "b.txt").unlink()
    results = pipeline().run([_spec("a"), _spec("b"), _spec("c", options={"fail": True})], RunOptions())
    assert [r.status for r in results] == [DocumentStatus.INGESTED, DocumentStatus.FAILED, DocumentStatus.FAILED]
    assert "source file not found" in (results[1].error or "")
    assert runs.finished == [RunStatus.PARTIAL]
    assert list(documents.docs) == ["a"]


def test_quality_gate_failure_keeps_the_previous_version(env, tmp_path):
    pipeline, documents, *_ = env
    pipeline().run([_spec("a")], RunOptions())
    (tmp_path / "a.txt").write_text("   \n", encoding="utf-8")  # now parses to nothing
    result = pipeline().run([_spec("a")], RunOptions())[0]
    assert result.status == DocumentStatus.FAILED and "quality gate" in (result.error or "")
    assert len(documents.docs["a"][1]) == 2  # old chunks still live


def test_disabled_sources_are_pruned_but_not_on_a_targeted_run(env):
    pipeline, documents, *_ = env
    pipeline().run([_spec("a"), _spec("b")], RunOptions())
    specs = [_spec("a"), _spec("b", enabled=False)]
    assert pipeline().run(specs, RunOptions(source_ids=["a"]))[0].status == DocumentStatus.UNCHANGED
    assert set(documents.docs) == {"a", "b"}
    statuses = {r.source_id: r.status for r in pipeline().run(specs, RunOptions())}
    assert statuses == {"a": DocumentStatus.UNCHANGED, "b": DocumentStatus.PRUNED}
    assert set(documents.docs) == {"a"}


def test_dry_run_needs_no_store_and_writes_artifacts(env):
    pipeline, documents, _, embedder, artifacts, _ = env
    results = pipeline(with_store=False).run([_spec("a")], RunOptions(dry_run=True))
    assert [r.status for r in results] == [DocumentStatus.CHECKED]
    assert artifacts.written == ["a"] and embedder.embedded == 0 and not documents.docs


def test_full_run_without_a_store_is_a_configuration_error(env):
    pipeline, *_ = env
    with pytest.raises(ConfigurationError):
        pipeline(with_store=False).run([_spec("a")], RunOptions())


def test_registry_problems_fail_fast(env):
    pipeline, *_ = env
    with pytest.raises(RegistryError, match="unknown source ids"):
        pipeline().run([_spec("a")], RunOptions(source_ids=["zzz"]))
    with pytest.raises(ParseError, match="invalid options"):
        pipeline().run([_spec("a", options={"typo": 1})], RunOptions())
