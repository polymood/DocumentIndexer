"""Headless Tantivy index builder.

Stateless run helpers consumed by both the CLI and the GUI worker thread. The
public entry point is :func:`run`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tantivy

from document_indexer.indexer.schema import (
    FieldDef,
    IndexerConfig,
    coerce,
    extract_from_file,
    extract_from_obj,
)

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class IndexResult:
    """Counts produced by :func:`run`."""

    documents: int
    files: int
    cancelled: bool = False


def _noop_progress(_done: int, _total: int, _message: str) -> None:
    return None


def _noop_log(_line: str) -> None:
    return None


def _never_cancelled() -> bool:
    return False


def build_schema(fields: list[FieldDef]) -> tantivy.Schema:
    """Build a Tantivy schema from a list of :class:`FieldDef`."""
    builder = tantivy.SchemaBuilder()
    for fd in fields:
        if not fd.name:
            continue
        if fd.type == "text":
            builder.add_text_field(
                fd.name,
                stored=fd.stored,
                tokenizer_name=fd.tokenizer,
            )
        elif fd.type == "integer":
            builder.add_integer_field(fd.name, stored=fd.stored, indexed=fd.indexed, fast=fd.fast)
        elif fd.type == "date":
            builder.add_date_field(fd.name, stored=fd.stored, indexed=fd.indexed, fast=fd.fast)
    return builder.build()


def open_writer(config: IndexerConfig) -> tantivy.IndexWriter:
    """Create the index directory and return a writer ready to accept documents."""
    schema = build_schema(config.fields)
    out = Path(config.index_path)
    out.mkdir(parents=True, exist_ok=True)
    index = tantivy.Index(schema, path=str(out))
    heap = max(20, config.heap_mb) * 1024 * 1024
    try:
        return index.writer(heap_size=heap, num_threads=max(1, config.threads))
    except TypeError:
        return index.writer(heap_size=heap)


def _list_files(
    src: Path, pattern: str, recursive: bool, exclude: set[str] | None = None
) -> list[Path]:
    files = list(src.rglob(pattern) if recursive else src.glob(pattern))
    return [f for f in files if f.is_file() and (not exclude or f.name not in exclude)]


def _build_doc_from_file(fields: list[FieldDef], file_path: Path, root: Path) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    for fd in fields:
        if not fd.name:
            continue
        raw = extract_from_file(fd.source, fd.source_arg, file_path, root)
        value = coerce(raw, fd.type)
        if value is None or (isinstance(value, str) and value == ""):
            continue
        doc[fd.name] = value
    return doc


def _build_doc_from_obj(
    fields: list[FieldDef],
    obj: dict[str, Any],
    file_path: Path,
    root: Path,
) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    for fd in fields:
        if not fd.name:
            continue
        if fd.source == "json_key":
            raw = extract_from_obj(fd.source, fd.source_arg, obj, fd.name)
        elif fd.source == "literal":
            raw = fd.source_arg
        else:
            raw = extract_from_file(fd.source, fd.source_arg, file_path, root)
        value = coerce(raw, fd.type)
        if value is None or (isinstance(value, str) and value == ""):
            continue
        doc[fd.name] = value
    return doc


def _run_txt(
    config: IndexerConfig,
    writer: tantivy.IndexWriter,
    progress: ProgressCallback,
    log: LogCallback,
    cancelled: CancelCallback,
) -> IndexResult:
    src = Path(config.src_folder)
    files = _list_files(src, config.glob_pattern, config.recursive)
    total = len(files)
    if total == 0:
        raise FileNotFoundError(f"No files matched {config.glob_pattern} under {src}")

    log(f"TXT mode — {total} file(s). Output: {config.index_path}")
    progress(0, total, "starting")

    indexed = 0
    for i, file_path in enumerate(files, start=1):
        if cancelled():
            writer.rollback()
            log("Cancelled. Rolled back uncommitted writes.")
            return IndexResult(documents=indexed, files=i - 1, cancelled=True)
        try:
            doc_kwargs = _build_doc_from_file(config.fields, file_path, src)
            writer.add_document(tantivy.Document(**doc_kwargs))
            indexed += 1
        except Exception as exc:
            log(f"  skip {file_path.name}: {exc}")
        if i % 25 == 0 or i == total:
            progress(i, total, file_path.name)

    log("Committing…")
    writer.commit()
    writer.wait_merging_threads()
    return IndexResult(documents=indexed, files=total)


def _run_ndjson(
    config: IndexerConfig,
    writer: tantivy.IndexWriter,
    progress: ProgressCallback,
    log: LogCallback,
    cancelled: CancelCallback,
) -> IndexResult:
    src = Path(config.src_folder)
    pattern = config.glob_pattern if config.glob_pattern.endswith(".ndjson") else "*.ndjson"
    files = _list_files(src, pattern, config.recursive, exclude={"schema.ndjson"})
    if not files:
        raise FileNotFoundError(f"No NDJSON data files matched {pattern} under {src}")

    log(f"NDJSON mode — counting lines in {len(files)} file(s)…")
    total = 0
    for file_path in files:
        if cancelled():
            return IndexResult(documents=0, files=0, cancelled=True)
        try:
            with file_path.open("rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            continue

    if total == 0:
        raise ValueError("No JSON lines found.")

    log(f"Total docs: {total}. Output: {config.index_path}")
    progress(0, total, "starting")

    indexed = 0
    done = 0
    for file_path in files:
        if cancelled():
            writer.rollback()
            log("Cancelled. Rolled back uncommitted writes.")
            return IndexResult(documents=indexed, files=0, cancelled=True)
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    if cancelled():
                        writer.rollback()
                        log("Cancelled. Rolled back uncommitted writes.")
                        return IndexResult(documents=indexed, files=0, cancelled=True)
                    line = raw.strip()
                    done += 1
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        log(f"  bad JSON {file_path.name}:{line_no} — {exc}")
                        continue
                    if not isinstance(obj, dict):
                        continue
                    try:
                        doc_kwargs = _build_doc_from_obj(config.fields, obj, file_path, src)
                        writer.add_document(tantivy.Document(**doc_kwargs))
                        indexed += 1
                    except Exception as exc:
                        log(f"  add_document failed at {file_path.name}:{line_no} — {exc}")
                    if done % 200 == 0:
                        progress(done, total, f"{file_path.name}:{line_no}")
        except OSError as exc:
            log(f"  skip {file_path.name}: {exc}")

    progress(done, total, "committing")
    writer.commit()
    writer.wait_merging_threads()
    return IndexResult(documents=indexed, files=len(files))


def validate(config: IndexerConfig) -> None:
    """Validate the configuration before opening writers; raises ``ValueError``."""
    if not config.src_folder:
        raise ValueError("Source folder is required.")
    if not Path(config.src_folder).is_dir():
        raise ValueError(f"Source folder not found: {config.src_folder}")
    if not config.index_path:
        raise ValueError("Output index path is required.")
    if not config.fields:
        raise ValueError("At least one field must be defined.")
    names = [f.name for f in config.fields if f.name]
    if len(set(names)) != len(names):
        raise ValueError("Field names must be unique.")


def run(
    config: IndexerConfig,
    *,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> IndexResult:
    """Execute an indexing job described by ``config``."""
    validate(config)
    progress = progress or _noop_progress
    log = log or _noop_log
    cancelled = cancelled or _never_cancelled

    writer = open_writer(config)
    if config.input_mode == "ndjson":
        return _run_ndjson(config, writer, progress, log, cancelled)
    return _run_txt(config, writer, progress, log, cancelled)
