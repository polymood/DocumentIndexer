"""Smoke tests for the Tantivy-backed index."""

from __future__ import annotations

from pathlib import Path

from document_indexer.indexer.db import Index


def test_add_and_search(tmp_path: Path) -> None:
    index = Index(tmp_path / "idx")
    index.add_document(
        source_path="/tmp/example.txt",
        title="Example Document",
        content="The quick brown fox jumps over the lazy dog.",
        metadata={"tag": "smoke"},
        word_count=9,
    )
    index.add_document(
        source_path="/tmp/other.txt",
        title="Other",
        content="A different sentence about radios and broadcasts.",
        metadata={"tag": "smoke"},
    )
    index.commit()

    assert index.document_count() == 2

    hits = index.search("fox")
    assert len(hits) == 1
    assert hits[0].title == "Example Document"

    hits = index.search("radios")
    assert len(hits) == 1
    assert hits[0].title == "Other"


def test_replace_on_same_source(tmp_path: Path) -> None:
    index = Index(tmp_path / "idx")
    index.add_document(source_path="/tmp/a.txt", title="A", content="alpha beta")
    index.commit()
    index.add_document(source_path="/tmp/a.txt", title="A v2", content="gamma delta")
    index.commit()

    assert index.document_count() == 1
    assert not index.search("alpha")
    hits = index.search("gamma")
    assert len(hits) == 1
    assert hits[0].title == "A v2"
