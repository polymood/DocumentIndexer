"""Tests for field extractors, coercion, and schema.ndjson loading."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from document_indexer.indexer.schema import (
    FieldDef,
    IndexerConfig,
    coerce,
    extract_from_file,
    extract_from_obj,
    load_ndjson_schema,
)


def test_extract_from_file_basics(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "Doc-Name.txt"
    target.parent.mkdir()
    target.write_text("hello world", encoding="utf-8")

    assert extract_from_file("file_content", "", target, tmp_path) == "hello world"
    assert extract_from_file("filename", "", target, tmp_path) == "Doc-Name.txt"
    assert extract_from_file("filename_stem", "", target, tmp_path) == "Doc-Name"
    assert extract_from_file("parent_dir", "", target, tmp_path) == "sub"
    assert extract_from_file("relative_path", "", target, tmp_path) == "sub/Doc-Name.txt"
    assert extract_from_file("full_path", "", target, tmp_path) == str(target)
    assert extract_from_file("literal", "abc", target, tmp_path) == "abc"


def test_extract_regex_filename(tmp_path: Path) -> None:
    target = tmp_path / "issue-1972-04.txt"
    target.write_text("x", encoding="utf-8")
    assert extract_from_file("regex_filename", r"(\d{4})", target, tmp_path) == "1972"
    assert extract_from_file("regex_filename", r"(\d{4})-(\d{2})||2", target, tmp_path) == "04"
    assert extract_from_file("regex_filename", r"", target, tmp_path) == ""


def test_extract_from_obj() -> None:
    obj = {"title": "Hi", "year": 1972}
    assert extract_from_obj("json_key", "title", obj, "title") == "Hi"
    assert extract_from_obj("json_key", "", obj, "title") == "Hi"
    assert extract_from_obj("literal", "fixed", obj, "x") == "fixed"
    assert extract_from_obj("unknown", "", obj, "x") is None


@pytest.mark.parametrize(
    ("value", "type_", "expected"),
    [
        ("42", "integer", 42),
        ("not-a-number", "integer", None),
        (None, "integer", None),
        ("2026-05-13", "date", datetime.fromisoformat("2026-05-13T00:00:00")),
        ("garbage", "date", None),
        (123, "text", "123"),
    ],
)
def test_coerce(value: object, type_: str, expected: object) -> None:
    assert coerce(value, type_) == expected  # type: ignore[arg-type]


def test_load_ndjson_schema(tmp_path: Path) -> None:
    schema_file = tmp_path / "schema.ndjson"
    schema_file.write_text(
        "\n".join(
            [
                json.dumps({"name": "body", "type": "text", "source": "json_key"}),
                "// comment",
                "",
                json.dumps(
                    {
                        "name": "year",
                        "type": "integer",
                        "stored": True,
                        "fast": True,
                        "source": "json_key",
                        "source_arg": "year",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    fields = load_ndjson_schema(schema_file)
    assert [f.name for f in fields] == ["body", "year"]
    assert fields[1].type == "integer"
    assert fields[1].fast is True


def test_indexer_config_roundtrip() -> None:
    cfg = IndexerConfig(
        src_folder="/x", index_path="/y", fields=[FieldDef(name="body", source="file_content")]
    )
    restored = IndexerConfig.from_dict(cfg.to_dict())
    assert restored == cfg
