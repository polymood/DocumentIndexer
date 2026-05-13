"""Command-line entry point for the OCR pipeline."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger

from document_indexer import __version__
from document_indexer.ocr.extract import extract
from document_indexer.ocr.lang import warm_up_detector
from document_indexer.ocr.output import write_ndjson_record, write_txt
from document_indexer.ocr.tesseract import MAX_OCR_WORKERS, clamp_workers, resolve_lang


def _configure_logger(verbose: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        level=level,
        colorize=True,
    )


def _iter_pdfs(inputs: Iterable[Path]) -> Iterable[Path]:
    for entry in inputs:
        if entry.is_file() and entry.suffix.lower() == ".pdf":
            yield entry
        elif entry.is_dir():
            yield from sorted(entry.rglob("*.pdf"))
        else:
            logger.warning(f"Skipping non-PDF input: {entry}")


def _common_root(paths: list[Path]) -> Path:
    if len(paths) == 1:
        path = paths[0]
        return path if path.is_dir() else path.parent
    resolved = [p.resolve() for p in paths]
    try:
        from os.path import commonpath

        return Path(commonpath([str(p) for p in resolved]))
    except ValueError:
        return Path.cwd()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docindex-ocr",
        description="Extract text from PDFs using a hybrid text-layer / Tesseract OCR pipeline.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF files or directories.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Output directory. Directory layout mirrors the input root.",
    )
    parser.add_argument(
        "--format",
        choices=["txt", "ndjson"],
        default="txt",
        help="Output format. NDJSON writes one record per PDF into <output>/documents.ndjson.",
    )
    parser.add_argument(
        "--ocr-lang",
        default="eng+deu",
        help="Tesseract languages (e.g. 'eng', 'eng+deu'). Pass 'auto' for all installed.",
    )
    parser.add_argument(
        "--ocr-dpi", "--dpi", type=int, default=200, help="DPI for OCR rendering (default 200)."
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=MAX_OCR_WORKERS,
        help=f"Threads for page-level OCR (default {MAX_OCR_WORKERS} = CPU count).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=24,
        help="Pages rendered per OCR batch (default 24).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel document workers (default 1; one PDF at a time uses all cores per page).",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Run OCR on every page, ignoring the embedded text layer.",
    )
    parser.add_argument(
        "--auto-lang",
        action="store_true",
        help="Per-document language detection via folder map and lingua.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)."
    )
    parser.add_argument("--version", action="version", version=f"docindex-ocr {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``docindex-ocr`` script."""
    args = _build_parser().parse_args(argv)
    _configure_logger(args.verbose)

    pdf_paths = list(_iter_pdfs(args.inputs))
    if not pdf_paths:
        logger.error("No PDFs found in the provided inputs.")
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    input_root = _common_root(args.inputs)
    ndjson_path = args.output / "documents.ndjson"

    ocr_lang = resolve_lang(args.ocr_lang)
    if args.auto_lang:
        warm_up_detector()

    logger.info(
        f"docindex-ocr {__version__} | {len(pdf_paths)} PDF(s) | "
        f"format={args.format} | workers={args.workers} | "
        f"ocr-workers={clamp_workers(args.ocr_workers)} | dpi={args.ocr_dpi}"
    )

    done = failed = 0

    def _run(pdf_path: Path) -> bool:
        result = extract(
            pdf_path,
            ocr_lang=ocr_lang,
            ocr_dpi=args.ocr_dpi,
            ocr_workers=args.ocr_workers,
            page_chunk=args.max_pages,
            force_ocr=args.force_ocr,
            auto_lang=args.auto_lang,
        )
        if not result.succeeded:
            return False
        if args.format == "txt":
            out_path = write_txt(result, args.output, input_root)
            logger.info(
                f"[{result.method}] {result.page_count}p {result.word_count:,}w -> {out_path}"
            )
        else:
            write_ndjson_record(result, ndjson_path)
            logger.info(
                f"[{result.method}] {result.page_count}p {result.word_count:,}w -> "
                f"{ndjson_path.name}"
            )
        return True

    workers = max(1, args.workers)
    if workers == 1:
        for path in pdf_paths:
            if _run(path):
                done += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run, path): path for path in pdf_paths}
            for fut in as_completed(futures):
                if fut.result():
                    done += 1
                else:
                    failed += 1

    logger.info(f"Done. Extracted: {done}, failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
