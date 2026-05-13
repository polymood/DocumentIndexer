"""End-to-end smoke tests for the headless indexer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tantivy

from document_indexer.indexer.builder import build_schema, run, validate
from document_indexer.indexer.schema import (
    FieldDef,
    IndexerConfig,
    default_ndjson_fields,
    default_txt_fields,
)


def _docs_in(index_path: Path, fields: list[FieldDef]) -> int:
    index = tantivy.Index(build_schema(fields), path=str(index_path))
    index.reload()
    return int(index.searcher().num_docs)


def test_validate_rejects_missing_src(tmp_path: Path) -> None:
    cfg = IndexerConfig(
        src_folder="", index_path=str(tmp_path / "idx"), fields=default_txt_fields()
    )
    with pytest.raises(ValueError, match="Source folder"):
        validate(cfg)


def test_validate_rejects_duplicate_field_names(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    cfg = IndexerConfig(
        src_folder=str(tmp_path / "src"),
        index_path=str(tmp_path / "idx"),
        fields=[FieldDef(name="body"), FieldDef(name="body")],
    )
    with pytest.raises(ValueError, match="unique"):
        validate(cfg)


def test_run_txt(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha mike foxtrot", encoding="utf-8")
    (src / "b.txt").write_text("tango uniform victor", encoding="utf-8")

    cfg = IndexerConfig(
        src_folder=str(src),
        index_path=str(tmp_path / "idx"),
        glob_pattern="*.txt",
        recursive=True,
        input_mode="txt",
        fields=default_txt_fields(),
    )
    result = run(cfg)
    assert result.documents == 2
    assert _docs_in(Path(cfg.index_path), cfg.fields) == 2


def test_run_ndjson(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    records = [
        {"body": "alpha mike foxtrot", "source_url": "/a"},
        {"body": "tango uniform victor", "source_url": "/b"},
    ]
    (src / "docs.ndjson").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    cfg = IndexerConfig(
        src_folder=str(src),
        index_path=str(tmp_path / "idx"),
        glob_pattern="*.ndjson",
        recursive=True,
        input_mode="ndjson",
        fields=default_ndjson_fields(),
    )
    result = run(cfg)
    assert result.documents == 2
    assert _docs_in(Path(cfg.index_path), cfg.fields) == 2
