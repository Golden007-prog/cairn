"""#2451 — the RAG ingest service's parse step after `rag/parse` was retired (#2413).

The service no longer looks the retired operator up, and it never tells a user to install
`operator-rag-parse`, a package that is gone. What parses today: the built-in readers, and
LlamaParse for PDF when `LLAMA_CLOUD_API_KEY` is set. A PDF without that key, and an Office
file (no parser ships), fail with `UnparseableDocument` — so the indexer records them as failed
and retries them — instead of being skipped as "indexed, 0 chunks" or embedded as zip bytes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("llama_index.core", reason="the rag extra is not installed")

from llama_index.core.workflow import StartEvent  # noqa: E402

import cairn.services.rag.ingest as ingest  # noqa: E402


class _Store:
    async def has_source(self, source: str) -> bool:
        return False


@pytest.fixture
def workflow(monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: SimpleNamespace(llama_cloud_api_key=None))
    return ingest.IngestWorkflow(store=_Store(), embedder=None)


async def test_a_pdf_without_a_llamaparse_key_fails_saying_what_to_set_not_a_retired_package(workflow, tmp_path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    with pytest.raises(ingest.UnparseableDocument) as err:
        await workflow.parse(StartEvent(file_path=str(pdf)))
    assert "LLAMA_CLOUD_API_KEY" in str(err.value) and "operator-rag-parse" not in str(err.value)


@pytest.mark.parametrize("ext", [".docx", ".pptx", ".xlsx"])
async def test_an_office_file_fails_with_the_reason_not_embedded_as_zip_bytes(workflow, tmp_path, ext):
    doc = tmp_path / f"runbook{ext}"
    doc.write_bytes(b"PK\x03\x04\x14\x00\x06\x00binary office zip")
    with pytest.raises(ingest.UnparseableDocument, match="no Office parser") as err:
        await workflow.parse(StartEvent(file_path=str(doc)))
    assert "operator-rag-parse" not in str(err.value)


async def test_the_built_in_readers_still_parse_markdown(workflow, tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# Failover\n\nPromote the replica.\n", encoding="utf-8")
    out = await workflow.parse(StartEvent(file_path=str(md)))
    assert out.source_type == "markdown" and "Promote the replica." in out.docs[0].text



class _IndexStore:
    """The vector store the real ingest workflow writes to, in memory."""

    def __init__(self):
        self.rows: list[dict] = []

    async def initialize(self, collection, dim):
        pass

    async def has_source(self, source):
        return False

    async def delete_by_source(self, source):
        return 0

    async def upsert(self, ids, dense, sparse, payloads):
        self.rows.extend(payloads)
        return len(ids)


class _IndexEmbedder:
    dim = 4

    async def embed(self, texts):
        return [[0.1] * self.dim for _ in texts]

    async def embed_sparse(self, texts):
        return [None for _ in texts]


async def test_the_indexer_records_an_unparseable_file_as_failed_and_retries_it(tmp_path, monkeypatch):
    """Through the indexer and the REAL ingest workflow: the markdown file is indexed with its
    chunks; the PDF (no LlamaParse key) and the Office file land in `failed_documents` with
    catalog status `failed` — not counted indexed with 0 chunks — and the next run retries them
    while it skips the indexed markdown."""
    import cairn.services.rag.embeddings as embeddings
    import cairn.services.rag.indexer as indexer
    import cairn.services.rag.stores as stores
    from cairn.core.storage.rag_catalog import InMemoryRagCatalog

    store, embedder = _IndexStore(), _IndexEmbedder()

    async def _no_connection(*a, **k):
        return None

    for module, attr, value in (
        (ingest, "get_settings", lambda: SimpleNamespace(llama_cloud_api_key=None, rag_collection_name="kb")),
        (ingest, "get_vector_store", lambda: store), (ingest, "get_embedder", lambda: embedder),
        (ingest, "apply_backend_connection", _no_connection), (indexer, "apply_backend_connection", _no_connection),
        (stores, "get_vector_store", lambda: store), (embeddings, "get_embedder", lambda: embedder),
    ):
        monkeypatch.setattr(module, attr, value)
    (tmp_path / "notes.md").write_text("# Failover\n\nPromote the replica.\n", encoding="utf-8")
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    (tmp_path / "deck.pptx").write_bytes(b"PK\x03\x04\x14\x00\x06\x00binary office zip")
    catalog = InMemoryRagCatalog()

    first = await indexer.index_corpus("kb", str(tmp_path), catalog=catalog)
    assert first.status == "partially_completed" and first.documents_indexed == 1 and first.chunks_indexed >= 1
    failed = {f["source"]: f["error"] for f in first.failed_documents}
    assert set(failed) == {str(tmp_path / "spec.pdf"), str(tmp_path / "deck.pptx")}, first
    assert "LLAMA_CLOUD_API_KEY" in failed[str(tmp_path / "spec.pdf")]
    assert "no Office parser" in failed[str(tmp_path / "deck.pptx")]
    status = {e.source: (e.status, e.chunk_count) for e in await catalog.list_by_corpus("kb")}
    assert status[str(tmp_path / "notes.md")][0] == "indexed" and status[str(tmp_path / "notes.md")][1] >= 1
    assert status[str(tmp_path / "spec.pdf")][0] == status[str(tmp_path / "deck.pptx")][0] == "failed"
    assert {r["source"] for r in store.rows} == {str(tmp_path / "notes.md")}  # no zip bytes embedded

    again = await indexer.index_corpus("kb", str(tmp_path), catalog=catalog)
    assert again.documents_skipped == 1  # the indexed markdown
    assert {f["source"] for f in again.failed_documents} == set(failed)  # retried, still failing loudly
