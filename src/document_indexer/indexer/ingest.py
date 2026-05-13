"""Ingest ``.txt`` and ``.ndjson`` files into the Tantivy index."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from document_indexer.indexer.db import Index


@dataclass(slots=True)
class IngestStats:
    """Counts collected by :func:`ingest`."""

    files: int = 0
    documents: int = 0
    errors: int = 0


Format = Literal["auto", "txt", "ndjson"]


def _iter_inputs(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from sorted(path.rglob("*.txt"))
        yield from sorted(path.rglob("*.ndjson"))
    else:
        logger.warning(f"Skipping non-existent input: {path}")


def _detect_format(path: Path) -> Format:
    suffix = path.suffix.lower()
    if suffix == ".ndjson":
        return "ndjson"
    if suffix == ".txt":
        return "txt"
    return "auto"


def _ingest_txt(index: Index, path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    index.add_document(
        source_path=str(path.resolve()),
        title=path.stem,
        content=text,
        metadata={"filename": path.name, "parent": path.parent.name},
        word_count=len(text.split()),
        filename=path.name,
        parent=path.parent.name,
    )
    return 1


def _ingest_ndjson(index: Index, path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(f"{path}:{line_no}: invalid JSON ({exc})")
                continue
            text = record.pop("text", "") or ""
            stats = record.pop("stats", {}) or {}
            source_path = record.get("source_path") or record.get("path") or f"{path}#L{line_no}"
            source_path_resolved = (
                str(Path(source_path).resolve()) if Path(source_path).exists() else str(source_path)
            )
            title = (
                record.get("title")
                or record.get("stem")
                or record.get("filename")
                or Path(source_path).stem
            )
            index.add_document(
                source_path=source_path_resolved,
                title=str(title),
                content=text,
                metadata={**record, "_source_file": str(path.resolve())},
                page_count=stats.get("page_count"),
                word_count=stats.get("word_count") or len(text.split()),
                filename=record.get("filename") or Path(source_path).name,
                parent=record.get("parent") or Path(source_path).parent.name,
            )
            count += 1
    return count


def ingest(
    index: Index,
    inputs: Iterable[Path],
    format: Format = "auto",
) -> IngestStats:
    """Ingest a list of files or directories into ``index``."""
    stats = IngestStats()
    for entry in inputs:
        for path in _iter_inputs(entry):
            chosen = format if format != "auto" else _detect_format(path)
            try:
                if chosen == "ndjson":
                    stats.documents += _ingest_ndjson(index, path)
                elif chosen == "txt":
                    stats.documents += _ingest_txt(index, path)
                else:
                    logger.warning(f"Unknown format for {path}; skipping.")
                    continue
                stats.files += 1
            except (OSError, ValueError) as exc:
                logger.error(f"Failed to ingest {path}: {exc}")
                stats.errors += 1
    index.commit()
    return stats
