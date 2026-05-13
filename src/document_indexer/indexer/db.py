"""Tantivy-backed index for DocumentIndexer.

The index is a directory of Tantivy segments. The schema is intentionally fixed
to the output of ``docindex-ocr``; extra NDJSON fields are serialised into a
``metadata`` text field that is stored but not indexed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tantivy

WRITER_HEAP_BYTES: int = 256 * 1024 * 1024


@dataclass(slots=True)
class SearchHit:
    """One result row returned by :meth:`Index.search`."""

    title: str
    source_path: str
    snippet: str
    score: float
    metadata: dict[str, Any]
    page_count: int | None
    word_count: int | None


def _build_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("body", stored=True, tokenizer_name="default")
    builder.add_text_field("title", stored=True, tokenizer_name="default")
    builder.add_text_field("source_path", stored=True, tokenizer_name="raw")
    builder.add_text_field("filename", stored=True, tokenizer_name="default")
    builder.add_text_field("parent", stored=True, tokenizer_name="raw")
    builder.add_text_field("metadata", stored=True, tokenizer_name="raw")
    builder.add_integer_field("page_count", stored=True, indexed=False, fast=True)
    builder.add_integer_field("word_count", stored=True, indexed=False, fast=True)
    return builder.build()


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(retrieved: dict[str, Any], key: str) -> Any:
    """Tantivy retrieved documents store values as lists. Return the first."""
    values = retrieved.get(key)
    if isinstance(values, list) and values:
        return values[0]
    return values


class Index:
    """Tantivy index wrapper for the DocumentIndexer schema."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.mkdir(parents=True, exist_ok=True)
        self._schema = _build_schema()
        self._index = tantivy.Index(self._schema, path=str(path))
        self._writer: tantivy.IndexWriter | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _get_writer(self) -> tantivy.IndexWriter:
        if self._writer is None:
            self._writer = self._index.writer(heap_size=WRITER_HEAP_BYTES)
        return self._writer

    def add_document(
        self,
        *,
        source_path: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        page_count: int | None = None,
        word_count: int | None = None,
        filename: str | None = None,
        parent: str | None = None,
    ) -> None:
        """Add or replace a document keyed by ``source_path``."""
        writer = self._get_writer()
        writer.delete_documents("source_path", source_path)
        path = Path(source_path)
        doc_kwargs: dict[str, Any] = {
            "body": content,
            "title": title or path.stem,
            "source_path": source_path,
            "filename": filename or path.name,
            "parent": parent or path.parent.name,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
        }
        if page_count is not None:
            doc_kwargs["page_count"] = int(page_count)
        if word_count is not None:
            doc_kwargs["word_count"] = int(word_count)
        writer.add_document(tantivy.Document(**doc_kwargs))

    def commit(self) -> None:
        """Commit pending writes and refresh the index reader."""
        if self._writer is not None:
            self._writer.commit()
            self._writer.wait_merging_threads()
            self._writer = None
        self._index.reload()

    def document_count(self) -> int:
        self._index.reload()
        return int(self._index.searcher().num_docs)

    def search(self, query: str, limit: int = 50) -> list[SearchHit]:
        """Parse and run an FTS query against ``body`` and ``title``."""
        if not query.strip():
            return []
        self._index.reload()
        searcher = self._index.searcher()
        parsed = self._index.parse_query(query, ["body", "title"])
        snippet_generator = tantivy.SnippetGenerator.create(searcher, parsed, self._schema, "body")
        result = searcher.search(parsed, limit)
        hits: list[SearchHit] = []
        for score, doc_address in result.hits:
            retrieved = searcher.doc(doc_address)
            stored = retrieved.to_dict()
            snippet_html = snippet_generator.snippet_from_doc(retrieved).to_html()
            metadata_raw = _first(stored, "metadata") or "{}"
            try:
                metadata = json.loads(metadata_raw)
            except json.JSONDecodeError:
                metadata = {}
            hits.append(
                SearchHit(
                    title=str(_first(stored, "title") or ""),
                    source_path=str(_first(stored, "source_path") or ""),
                    snippet=snippet_html,
                    score=float(score),
                    metadata=metadata,
                    page_count=_coerce_int(_first(stored, "page_count")),
                    word_count=_coerce_int(_first(stored, "word_count")),
                )
            )
        return hits

    def close(self) -> None:
        """Commit any pending writes."""
        if self._writer is not None:
            self.commit()
