"""Tests for ingest of txt and ndjson into the Tantivy index."""

from __future__ import annotations

import json
from pathlib import Path

from document_indexer.indexer.db import Index
from document_indexer.indexer.ingest import ingest


def test_ingest_txt(tmp_path: Path) -> None:
    sample = tmp_path / "doc.txt"
    sample.write_text("hello world from the indexer", encoding="utf-8")

    index = Index(tmp_path / "idx")
    stats = ingest(index, [sample])

    assert stats.files == 1
    assert stats.documents == 1
    assert index.document_count() == 1
    assert len(index.search("indexer")) == 1


def test_ingest_ndjson(tmp_path: Path) -> None:
    sample = tmp_path / "docs.ndjson"
    records = [
        {"title": "T1", "source_path": "/x/a.pdf", "text": "alpha mike foxtrot"},
        {"title": "T2", "source_path": "/x/b.pdf", "text": "tango uniform victor"},
    ]
    sample.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    index = Index(tmp_path / "idx")
    stats = ingest(index, [sample])

    assert stats.files == 1
    assert stats.documents == 2
    assert index.document_count() == 2

    hits = index.search("foxtrot")
    assert len(hits) == 1
    assert hits[0].title == "T1"
