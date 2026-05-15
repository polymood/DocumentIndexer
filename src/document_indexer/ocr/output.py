"""Output writers for extraction results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from document_indexer.ocr.extract import ExtractionResult
from document_indexer.ocr.metadata import build_metadata


def write_txt(result: ExtractionResult, output_root: Path, input_root: Path) -> Path:
    """Write the extracted text to a ``.txt`` file mirroring the input layout."""
    rel = result.pdf_path.resolve().relative_to(input_root.resolve())
    out_path = output_root / rel.with_suffix(".txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.text, encoding="utf-8")
    return out_path


def write_ia_txt(
    result: ExtractionResult,
    output_root: Path,
    input_root: Path,
    identifier: str,
) -> Path:
    """Write IA item text to ``<output_root>/<rel-collection-dir>/<identifier>.txt``."""
    item_dir = result.pdf_path.parent if result.pdf_path is not None else input_root
    try:
        rel_dir = item_dir.resolve().relative_to(input_root.resolve()).parent
    except ValueError:
        rel_dir = Path()
    out_path = output_root / rel_dir / f"{identifier}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.text, encoding="utf-8")
    return out_path


def write_ndjson_record(
    result: ExtractionResult,
    out_path: Path,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one NDJSON record for ``result`` to ``out_path`` and return it."""
    record: dict[str, Any] = {
        **build_metadata(result.pdf_path, extra_metadata),
        "text": result.text,
        "stats": {
            "page_count": result.page_count,
            "char_count": result.char_count,
            "word_count": result.word_count,
            "file_size_bytes": result.file_size_bytes,
            "direct_pages": result.direct_pages,
            "ocr_pages": result.ocr_pages,
            "method": result.method,
            "language": result.language,
            "language_source": result.language_source,
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
