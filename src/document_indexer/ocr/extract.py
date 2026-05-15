"""PDF text-layer extraction and Tesseract OCR orchestration."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import fitz
import pytesseract
from loguru import logger
from PIL import Image, ImageOps

from document_indexer.ocr.ia import IAItem, read_djvu_text
from document_indexer.ocr.lang import detect_lang_from_text, lang_from_path
from document_indexer.ocr.tesseract import clamp_workers

fitz.TOOLS.mupdf_display_errors(False)
Image.MAX_IMAGE_PIXELS = None


TEXT_LAYER_THRESHOLD: int = 80
TESS_CONFIG: str = "--oem 1 --psm 3"


_FITZ_LOCK = threading.Lock()


@dataclass(slots=True)
class ExtractionResult:
    """Outcome of running the extraction pipeline on a single PDF."""

    pdf_path: Path
    text: str
    pages: list[str]
    method: str
    language: str
    language_source: str
    page_count: int
    char_count: int
    word_count: int
    file_size_bytes: int
    direct_pages: int
    ocr_pages: int
    error: str | None = field(default=None)

    @property
    def succeeded(self) -> bool:
        return self.error is None


def _extract_direct_per_page(pdf_path: Path) -> list[str]:
    with _FITZ_LOCK:
        doc = fitz.open(str(pdf_path))
        try:
            return [page.get_text() for page in doc]
        finally:
            doc.close()


def _render_pages_batch(pdf_path: Path, indices: list[int], dpi: int) -> list[Image.Image]:
    images: list[Image.Image] = []
    with _FITZ_LOCK:
        doc = fitz.open(str(pdf_path))
        try:
            for idx in indices:
                pix = doc[idx].get_pixmap(dpi=dpi, alpha=False)
                mode = "RGB" if pix.n >= 3 else "L"
                images.append(Image.frombytes(mode, (pix.width, pix.height), pix.samples))
        finally:
            doc.close()
    return images


def _ocr_one_page(args: tuple[int, Image.Image, str]) -> tuple[int, str]:
    idx, img, lang = args
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    text: str = pytesseract.image_to_string(img, lang=lang, config=TESS_CONFIG)
    return idx, text


def _ocr_pages(
    pdf_path: Path,
    indices: list[int],
    lang: str,
    dpi: int,
    workers: int,
    page_chunk: int,
) -> dict[int, str]:
    if not indices:
        return {}
    workers = clamp_workers(workers)
    sorted_indices = sorted(set(indices))
    chunk = page_chunk if page_chunk > 0 else len(sorted_indices)
    results: dict[int, str] = {}

    for start in range(0, len(sorted_indices), chunk):
        batch = sorted_indices[start : start + chunk]
        images = _render_pages_batch(pdf_path, batch, dpi)
        tasks = [(idx, img, lang) for idx, img in zip(batch, images, strict=True)]
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            for idx, text in pool.map(_ocr_one_page, tasks):
                results[idx] = text
        for img in images:
            img.close()

    return results


def _assemble(pages: list[str]) -> str:
    return "\n\n".join(f"[Page {i + 1}]\n{text}" for i, text in enumerate(pages))


def _resolve_ocr_lang(
    pdf_path: Path,
    page_texts: list[str],
    sparse_idx: list[int],
    fallback_lang: str,
    auto_lang: bool,
    sample_dpi: int,
) -> tuple[str, str, list[int], dict[int, str]]:
    """Resolve the language used for OCR.

    Returns ``(language, source, remaining_sparse, pre_ocred_pages)`` where
    ``pre_ocred_pages`` maps any pages already OCR'd during sampling.
    """
    if not auto_lang or not sparse_idx:
        return fallback_lang, "default", sparse_idx, {}

    mapped = lang_from_path(pdf_path)
    if mapped:
        return mapped, f"path-map[{pdf_path.parent.name}]", sparse_idx, {}

    direct_text = " ".join(text for i, text in enumerate(page_texts) if i not in set(sparse_idx))
    detected = detect_lang_from_text(direct_text)
    if detected:
        return detected, "direct-text", sparse_idx, {}

    sample_idx = sparse_idx[0]
    sample = _ocr_pages(pdf_path, [sample_idx], "eng", sample_dpi, 1, 1)
    sample_text = sample.get(sample_idx, "")
    detected = detect_lang_from_text(sample_text)
    if detected:
        remaining = [i for i in sparse_idx if i != sample_idx] if detected == "eng" else sparse_idx
        pre_ocred = {sample_idx: sample_text} if detected == "eng" else {}
        return detected, f"sample-page-{sample_idx + 1}", remaining, pre_ocred

    return fallback_lang, "fallback", sparse_idx, {}


def extract(
    pdf_path: Path,
    *,
    ocr_lang: str = "eng+deu",
    ocr_dpi: int = 200,
    ocr_workers: int = 1,
    page_chunk: int = 24,
    text_threshold: int = TEXT_LAYER_THRESHOLD,
    force_ocr: bool = False,
    auto_lang: bool = False,
) -> ExtractionResult:
    """Extract text from ``pdf_path`` using a hybrid direct+OCR strategy."""
    try:
        file_size = pdf_path.stat().st_size
        page_texts = _extract_direct_per_page(pdf_path)
        total_pages = len(page_texts)
        if total_pages == 0:
            raise RuntimeError("0-page PDF")

        if force_ocr:
            sparse_idx = list(range(total_pages))
        else:
            sparse_idx = [
                i for i, text in enumerate(page_texts) if len(text.strip()) < text_threshold
            ]
        direct_count = total_pages - len(sparse_idx)

        if not sparse_idx:
            text = _assemble(page_texts)
            return ExtractionResult(
                pdf_path=pdf_path,
                text=text,
                pages=page_texts,
                method="direct",
                language="",
                language_source="n/a",
                page_count=total_pages,
                char_count=len(text),
                word_count=len(text.split()),
                file_size_bytes=file_size,
                direct_pages=total_pages,
                ocr_pages=0,
            )

        language, source, remaining_sparse, pre_ocred = _resolve_ocr_lang(
            pdf_path=pdf_path,
            page_texts=page_texts,
            sparse_idx=sparse_idx,
            fallback_lang=ocr_lang,
            auto_lang=auto_lang,
            sample_dpi=ocr_dpi,
        )
        for idx, text in pre_ocred.items():
            page_texts[idx] = text

        effective_workers = clamp_workers(ocr_workers)
        method = "ocr" if direct_count == 0 or force_ocr else "hybrid"
        logger.info(
            f"{pdf_path.name}: {method} "
            f"(direct {direct_count}p, OCR {len(remaining_sparse)}p, "
            f"lang={language} via {source}, workers={effective_workers})"
        )

        ocr_results = _ocr_pages(
            pdf_path=pdf_path,
            indices=remaining_sparse,
            lang=language,
            dpi=ocr_dpi,
            workers=effective_workers,
            page_chunk=page_chunk,
        )
        for idx, text in ocr_results.items():
            page_texts[idx] = text

        text = _assemble(page_texts)
        return ExtractionResult(
            pdf_path=pdf_path,
            text=text,
            pages=page_texts,
            method=method,
            language=language,
            language_source=source,
            page_count=total_pages,
            char_count=len(text),
            word_count=len(text.split()),
            file_size_bytes=file_size,
            direct_pages=direct_count,
            ocr_pages=len(remaining_sparse) + len(pre_ocred),
        )

    except Exception as exc:
        logger.error(f"{pdf_path.name}: extraction failed: {exc}")
        return ExtractionResult(
            pdf_path=pdf_path,
            text="",
            pages=[],
            method="failed",
            language="",
            language_source="n/a",
            page_count=0,
            char_count=0,
            word_count=0,
            file_size_bytes=pdf_path.stat().st_size if pdf_path.exists() else 0,
            direct_pages=0,
            ocr_pages=0,
            error=str(exc),
        )


def extract_from_ia_item(item: IAItem) -> ExtractionResult:
    """Build an :class:`ExtractionResult` from a pre-existing IA djvu.txt file.

    No PDF rendering, no Tesseract call. Page count, where known, is taken
    from form-feed delimiters; otherwise 1.
    """
    if item.djvu_txt_path is None:
        raise ValueError(f"IAItem {item.identifier} has no djvu.txt to import")
    try:
        text = read_djvu_text(item.djvu_txt_path)
    except OSError as exc:
        return ExtractionResult(
            pdf_path=item.djvu_txt_path,
            text="",
            pages=[],
            method="failed",
            language="",
            language_source="n/a",
            page_count=0,
            char_count=0,
            word_count=0,
            file_size_bytes=0,
            direct_pages=0,
            ocr_pages=0,
            error=str(exc),
        )

    pages = text.split("\x0c") if "\x0c" in text else [text]
    page_count = len(pages)
    return ExtractionResult(
        pdf_path=item.pdf_path or item.djvu_txt_path,
        text=text,
        pages=pages,
        method="djvu-import",
        language="",
        language_source="ia-djvu",
        page_count=page_count,
        char_count=len(text),
        word_count=len(text.split()),
        file_size_bytes=item.djvu_txt_path.stat().st_size,
        direct_pages=page_count,
        ocr_pages=0,
    )
