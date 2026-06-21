"""Command-line entry point for the OCR pipeline."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from document_indexer import __version__
from document_indexer.ocr.extract import extract, extract_from_ia_item
from document_indexer.ocr.ia import IAItem, is_ia_item, iter_ia_items, parse_meta_xml
from document_indexer.ocr.lang import warm_up_detector
from document_indexer.ocr.output import write_ia_txt, write_ndjson_record, write_txt
from document_indexer.ocr.progress import PROGRESS_FILENAME, ProgressTracker
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
        "--ocr-backend",
        choices=["tesseract", "locro"],
        default="tesseract",
        help=(
            "OCR engine. 'tesseract' (default) uses installed Tesseract; "
            "'locro' uses Chrome's screen-ai via the clv-locro wrapper "
            "(install clv-locro from https://github.com/sergiocorreia/clv-locro)."
        ),
    )
    parser.add_argument(
        "--ocr-lang",
        default="eng+deu",
        help="Tesseract languages (e.g. 'eng', 'eng+deu'). Pass 'auto' for all installed. Ignored for --ocr-backend locro.",
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
        "--worker-mode",
        choices=["thread", "process"],
        default="thread",
        help=(
            "How to parallelise document workers. 'thread' (default) shares "
            "the engine across workers -- fine for tesseract, useless for "
            "backends that hold a global lock (locro). "
            "'process' spawns one OS process per worker, each with its own "
            "engine, giving real parallelism at the cost of N × engine memory."
        ),
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Run OCR on every page, ignoring the embedded text layer.",
    )
    parser.add_argument(
        "--force-text",
        action="store_true",
        help=(
            "Text-layer only: read embedded text from every PDF and never run "
            "OCR. Pages without a text layer come back empty. Mutually "
            "exclusive with --force-ocr."
        ),
    )
    parser.add_argument(
        "--auto-lang",
        action="store_true",
        help="Per-document language detection via folder map and lingua.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Disable resume from progress ledger; reprocess every input even "
            "if a previous run completed it."
        ),
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help=(
            "Delete the progress ledger in <output> before starting. Forces a "
            "full reprocess but keeps existing output files in place."
        ),
    )
    parser.add_argument(
        "--ia",
        choices=["auto", "on", "off"],
        default="auto",
        help=(
            "Internet Archive layout. 'auto' (default) inspects each input dir for IA "
            "items; 'on' forces IA mode; 'off' falls back to plain *.pdf glob."
        ),
    )
    parser.add_argument(
        "--ia-prefer-djvu",
        action="store_true",
        default=True,
        help="Use the existing _djvu.txt when present; only OCR the PDF when missing.",
    )
    parser.add_argument(
        "--ia-force-ocr",
        action="store_true",
        help="Ignore the IA _djvu.txt and re-run OCR on every PDF.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging (DEBUG level)."
    )
    parser.add_argument("--version", action="version", version=f"docindex-ocr {__version__}")
    return parser


def _detect_ia_mode(inputs: list[Path], mode: str) -> bool:
    """Resolve the ``--ia`` flag value to a concrete True/False."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    for entry in inputs:
        if not entry.is_dir():
            continue
        if is_ia_item(entry):
            return True
        for child in entry.iterdir():
            if child.is_dir() and is_ia_item(child):
                return True
    return False


def _collect_ia_items(inputs: list[Path]) -> list[IAItem]:
    items: list[IAItem] = []
    seen: set[Path] = set()
    for entry in inputs:
        if not entry.is_dir():
            logger.warning(f"--ia mode skips non-directory input: {entry}")
            continue
        for item in iter_ia_items(entry):
            if item.directory in seen:
                continue
            seen.add(item.directory)
            items.append(item)
    return items


@dataclass(slots=True, frozen=True)
class PdfJobConfig:
    """Picklable bundle of per-PDF runner config (shared across all PDFs in a run)."""

    ocr_lang: str
    ocr_dpi: int
    ocr_workers: int
    page_chunk: int
    force_ocr: bool
    force_text: bool
    auto_lang: bool
    ocr_backend: str
    output_format: str
    output_dir: Path
    input_root: Path
    ndjson_path: Path
    verbose: bool


# In worker processes the backend engine is loaded lazily on the first task.
_WORKER_BACKEND: str | None = None


def _pdf_worker_init(config: PdfJobConfig) -> None:
    """Initializer for ProcessPoolExecutor workers.

    Sets backend-specific env vars (must happen *before* the backend module
    is imported) and reconfigures loguru for the worker process. Backend
    warm-up is deferred to the first task -- otherwise eight workers would
    all download/load models in parallel just to reach the same memory
    state.
    """
    global _WORKER_BACKEND

    _configure_logger(config.verbose)
    _WORKER_BACKEND = config.ocr_backend


def _warm_up_worker_backend(backend: str, verbose: bool) -> None:
    """Lazy backend warm-up inside a worker process."""
    if backend == "locro":
        from document_indexer.ocr.locro import reattach_loguru_sink, warm_up

        warm_up()
        reattach_loguru_sink(verbose=verbose)


def _pdf_worker(pdf_path: Path, config: PdfJobConfig) -> dict[str, object]:
    """Process one PDF inside a worker. Returns a status dict for the main process."""
    global _WORKER_BACKEND
    if _WORKER_BACKEND != "__warmed__":
        try:
            _warm_up_worker_backend(config.ocr_backend, config.verbose)
        except Exception as exc:
            return {"ok": False, "key": str(pdf_path.resolve()), "error": f"warm-up: {exc}"}
        _WORKER_BACKEND = "__warmed__"

    result = extract(
        pdf_path,
        ocr_lang=config.ocr_lang,
        ocr_dpi=config.ocr_dpi,
        ocr_workers=config.ocr_workers,
        page_chunk=config.page_chunk,
        force_ocr=config.force_ocr,
        force_text=config.force_text,
        auto_lang=config.auto_lang,
        ocr_backend=config.ocr_backend,
    )
    if not result.succeeded:
        return {
            "ok": False,
            "key": str(pdf_path.resolve()),
            "error": result.error or "extraction failed",
        }
    if config.output_format == "txt":
        out_path = write_txt(result, config.output_dir, config.input_root)
        target = str(out_path)
    else:
        write_ndjson_record(result, config.ndjson_path)
        target = config.ndjson_path.name
    logger.info(f"[{result.method}] {result.page_count}p {result.word_count:,}w -> {target}")
    return {
        "ok": True,
        "key": str(pdf_path.resolve()),
        "method": result.method,
        "pages": result.page_count,
        "words": result.word_count,
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``docindex-ocr`` script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logger(args.verbose)

    if args.force_ocr and args.force_text:
        parser.error("--force-ocr and --force-text are mutually exclusive")

    args.output.mkdir(parents=True, exist_ok=True)
    input_root = _common_root(args.inputs)
    ndjson_path = args.output / "documents.ndjson"

    progress = ProgressTracker(args.output / PROGRESS_FILENAME, enabled=not args.no_resume)
    if args.reset_progress:
        progress.reset()
        logger.info("progress: ledger reset")
    elif not args.no_resume and progress.completed_count:
        logger.info(
            f"progress: {progress.completed_count} previously-completed item(s) will be skipped"
        )

    ocr_lang = resolve_lang(args.ocr_lang) if args.ocr_backend == "tesseract" else ""

    if args.auto_lang:
        warm_up_detector()

    if args.ocr_backend == "locro":
        from document_indexer.ocr.locro import is_available as locro_available
        from document_indexer.ocr.locro import reattach_loguru_sink as locro_reattach
        from document_indexer.ocr.locro import warm_up as locro_warm_up

        if not locro_available():
            logger.error(
                "locro backend requires the `locro` package "
                "(https://github.com/sergiocorreia/clv-locro). "
                "Install with `pip install -e /path/to/clv-locro`."
            )
            return 1
        try:
            locro_warm_up()
        except RuntimeError as exc:
            logger.error(f"locro init failed: {exc}")
            return 1
        # locro redirected fd 2 during init -- re-point loguru at the
        # newly-installed sys.stderr so subsequent logs are visible.
        locro_reattach(verbose=args.verbose)
        if args.workers > 1:
            logger.warning(
                "locro DLL is not thread-safe; --workers > 1 will serialize "
                "on the engine lock. Use tesseract for parallel doc workers."
            )

    ia_mode = _detect_ia_mode(args.inputs, args.ia)

    if ia_mode:
        items = _collect_ia_items(args.inputs)
        if not items:
            logger.error("--ia mode found no Internet Archive item directories.")
            return 1
        with_djvu = sum(1 for it in items if it.has_djvu_text)
        logger.info(
            f"docindex-ocr {__version__} | IA mode | {len(items)} item(s) "
            f"({with_djvu} have djvu.txt) | format={args.format} | "
            f"workers={args.workers} | ocr-workers={clamp_workers(args.ocr_workers)} "
            f"| dpi={args.ocr_dpi}"
        )
        return _run_ia(args, items, input_root, ndjson_path, ocr_lang, progress)

    pdf_paths = list(_iter_pdfs(args.inputs))
    if not pdf_paths:
        logger.error("No PDFs found in the provided inputs.")
        return 1

    # Filter previously-completed inputs. Done strictly per-file: an entry
    # in the ledger only exists after the full extract + write succeeded.
    pending: list[Path] = []
    skipped = 0
    for path in pdf_paths:
        key = str(path.resolve())
        if progress.is_done(key):
            skipped += 1
            continue
        pending.append(path)
    if skipped:
        logger.info(f"resume: skipping {skipped} previously-completed PDF(s)")

    logger.info(
        f"docindex-ocr {__version__} | {len(pending)} PDF(s) to process "
        f"({len(pdf_paths)} total) | format={args.format} | workers={args.workers} | "
        f"ocr-workers={clamp_workers(args.ocr_workers)} | dpi={args.ocr_dpi}"
    )

    done = failed = 0

    job_config = PdfJobConfig(
        ocr_lang=ocr_lang,
        ocr_dpi=args.ocr_dpi,
        ocr_workers=args.ocr_workers,
        page_chunk=args.max_pages,
        force_ocr=args.force_ocr,
        force_text=args.force_text,
        auto_lang=args.auto_lang,
        ocr_backend=args.ocr_backend,
        output_format=args.format,
        output_dir=args.output,
        input_root=input_root,
        ndjson_path=ndjson_path,
        verbose=args.verbose,
    )

    def _record_result(res: dict[str, object]) -> bool:
        nonlocal done, failed
        if not res.get("ok"):
            failed += 1
            err = res.get("error", "")
            logger.error(f"failed: {res.get('key')}: {err}")
            return False
        progress.mark_done(
            str(res["key"]),
            method=str(res.get("method", "")),
            pages=int(res.get("pages", 0) or 0),
            words=int(res.get("words", 0) or 0),
        )
        done += 1
        return True

    def _run_single(path: Path) -> dict[str, object]:
        return _pdf_worker(path, job_config)

    workers = max(1, args.workers)
    if workers == 1:
        for path in pending:
            _record_result(_run_single(path))
    elif args.worker_mode == "process":
        # ProcessPoolExecutor with spawn so backends that mutate process-global
        # state (e.g. locro's fd-2 redirection + CDLL handle) cannot inherit a
        # half-initialised parent. Each worker warms up its backend lazily on
        # first task.
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=ctx,
            initializer=_pdf_worker_init,
            initargs=(job_config,),
        ) as pool:
            futures = {pool.submit(_pdf_worker, path, job_config): path for path in pending}
            for fut in as_completed(futures):
                try:
                    _record_result(fut.result())
                except Exception as exc:
                    failed += 1
                    logger.error(f"worker crashed: {futures[fut]}: {exc}")
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_single, path): path for path in pending}
            for fut in as_completed(futures):
                try:
                    _record_result(fut.result())
                except Exception as exc:
                    failed += 1
                    logger.error(f"worker crashed: {futures[fut]}: {exc}")

    logger.info(f"Done. Extracted: {done}, failed: {failed}, skipped(resumed): {skipped}")
    return 0 if failed == 0 else 2


def _run_ia(
    args: argparse.Namespace,
    items: list[IAItem],
    input_root: Path,
    ndjson_path: Path,
    ocr_lang: str,
    progress: ProgressTracker,
) -> int:
    # Filter previously-completed items. Key on item identifier so the same
    # item is recognised across runs regardless of cwd.
    pending: list[IAItem] = []
    skipped = 0
    for item in items:
        key = f"ia:{item.identifier}"
        if progress.is_done(key):
            skipped += 1
            continue
        pending.append(item)
    if skipped:
        logger.info(f"resume: skipping {skipped} previously-completed IA item(s)")

    done = failed = imported = ocred = 0

    def _process(item: IAItem) -> bool:
        nonlocal imported, ocred
        meta: dict[str, object] = {"ia_identifier": item.identifier}
        if item.meta_xml_path is not None:
            meta.update(parse_meta_xml(item.meta_xml_path))

        use_djvu = item.has_djvu_text and not args.ia_force_ocr and args.ia_prefer_djvu
        if use_djvu:
            result = extract_from_ia_item(item)
            imported += 1
        elif item.has_pdf:
            result = extract(
                item.pdf_path,  # type: ignore[arg-type]
                ocr_lang=ocr_lang,
                ocr_dpi=args.ocr_dpi,
                ocr_workers=args.ocr_workers,
                page_chunk=args.max_pages,
                force_ocr=args.force_ocr,
                force_text=args.force_text,
                auto_lang=args.auto_lang,
                ocr_backend=args.ocr_backend,
            )
            ocred += 1
        else:
            logger.warning(f"{item.identifier}: no djvu.txt and no PDF; skipping")
            return False

        if not result.succeeded:
            return False

        if args.format == "txt":
            out_path = write_ia_txt(result, args.output, input_root, item.identifier)
            logger.info(
                f"[{result.method}] {item.identifier}: "
                f"{result.page_count}p {result.word_count:,}w -> {out_path}"
            )
        else:
            write_ndjson_record(result, ndjson_path, extra_metadata=meta)
            logger.info(
                f"[{result.method}] {item.identifier}: "
                f"{result.page_count}p {result.word_count:,}w -> {ndjson_path.name}"
            )
        # Only after the output has been written.
        progress.mark_done(
            f"ia:{item.identifier}",
            method=result.method,
            pages=result.page_count,
            words=result.word_count,
        )
        return True

    workers = max(1, args.workers)
    if workers == 1:
        for item in pending:
            if _process(item):
                done += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process, item): item for item in pending}
            for fut in as_completed(futures):
                if fut.result():
                    done += 1
                else:
                    failed += 1

    logger.info(
        f"Done. Extracted: {done}, failed: {failed}, skipped(resumed): {skipped} "
        f"| djvu-import: {imported}, OCR'd: {ocred}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
