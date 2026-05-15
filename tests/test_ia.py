"""Tests for the Internet Archive layout helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path

from document_indexer.ocr.extract import extract_from_ia_item
from document_indexer.ocr.ia import (
    is_ia_item,
    iter_ia_items,
    load_ia_item,
    parse_meta_xml,
)


def _make_ia_item(directory: Path, identifier: str, *, with_djvu: bool = True) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{identifier}.pdf").write_bytes(b"%PDF-1.4\nstub\n")
    (directory / f"{identifier}_meta.xml").write_text(
        textwrap.dedent(
            f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <metadata>
              <identifier>{identifier}</identifier>
              <title>Sample Title</title>
              <creator>Some Author</creator>
              <date>1980-09-10</date>
              <collection>magazine_rack</collection>
              <collection>extra-collection</collection>
              <language>German</language>
              <ocr>tesseract 5.3.0</ocr>
            </metadata>
            """
        ),
        encoding="utf-8",
    )
    (directory / f"{identifier}_files.xml").write_text("<files/>", encoding="utf-8")
    if with_djvu:
        (directory / f"{identifier}_djvu.txt").write_text(
            "page one text\n\x0cpage two text\n",
            encoding="utf-8",
        )


def test_is_ia_item(tmp_path: Path) -> None:
    item_dir = tmp_path / "spex-1980-01"
    _make_ia_item(item_dir, "spex-1980-01")
    assert is_ia_item(item_dir)
    assert not is_ia_item(tmp_path)


def test_load_ia_item(tmp_path: Path) -> None:
    item_dir = tmp_path / "abc-001"
    _make_ia_item(item_dir, "abc-001")
    item = load_ia_item(item_dir)
    assert item is not None
    assert item.identifier == "abc-001"
    assert item.has_pdf
    assert item.has_djvu_text


def test_iter_ia_items_nested(tmp_path: Path) -> None:
    collection = tmp_path / "spex-zeitschrift"
    _make_ia_item(collection / "spex-1980-01", "spex-1980-01")
    _make_ia_item(collection / "spex-1980-02", "spex-1980-02", with_djvu=False)

    items = list(iter_ia_items(tmp_path))
    ids = sorted(item.identifier for item in items)
    assert ids == ["spex-1980-01", "spex-1980-02"]

    by_id = {item.identifier: item for item in items}
    assert by_id["spex-1980-01"].has_djvu_text
    assert not by_id["spex-1980-02"].has_djvu_text


def test_parse_meta_xml(tmp_path: Path) -> None:
    item_dir = tmp_path / "abc-001"
    _make_ia_item(item_dir, "abc-001")
    meta = parse_meta_xml(item_dir / "abc-001_meta.xml")
    assert meta["identifier"] == "abc-001"
    assert meta["title"] == "Sample Title"
    assert meta["creator"] == "Some Author"
    assert meta["language"] == "German"
    assert meta["collection"] == ["magazine_rack", "extra-collection"]


def test_extract_from_ia_item(tmp_path: Path) -> None:
    item_dir = tmp_path / "abc-001"
    _make_ia_item(item_dir, "abc-001")
    item = load_ia_item(item_dir)
    assert item is not None
    result = extract_from_ia_item(item)
    assert result.method == "djvu-import"
    assert result.page_count == 2
    assert "page one text" in result.text
    assert "page two text" in result.text
    assert result.error is None
