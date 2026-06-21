"""locro (Chrome screen-ai) OCR backend.

Wraps the `clv-locro` library (https://github.com/sergiocorreia/clv-locro) as a
drop-in alternative to Tesseract / DeepSeek inside the extraction pipeline.
Chrome's `screen-ai` engine is loaded via ctypes from a shared library
(`libchromescreenai.so` / `chrome_screen_ai.dll`), so no browser process is
required.

Concurrency: the underlying DLL keeps internal state across calls. We
serialize page calls via a single worker (clamp ocr_workers=1 when this
backend is selected). The CLI does this.

Setup notes:
  - Install: `pip install -e '.[locro]'` plus a local clone of clv-locro
    (`pip install -e /path/to/clv-locro`) -- the package is not on PyPI.
  - First run needs the screen-ai shared library + models. Either let locro
    auto-discover them from a Chrome install, point it at a directory with
    `LOCRO_MODEL_DIR`, or run `locro download` once.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from PIL import Image as PILImage


MODEL_DIR_ENV = "LOCRO_MODEL_DIR"
LIGHT_MODE = os.environ.get("LOCRO_LIGHT_MODE", "0") == "1"


_ENGINE = None
_LOAD_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def _load() -> object:
    """Load the ScreenAI engine (singleton)."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _LOAD_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            from locro import ScreenAI
        except ImportError as exc:
            raise RuntimeError(
                "locro backend needs the `locro` package. "
                "Install it from https://github.com/sergiocorreia/clv-locro "
                "(`pip install -e /path/to/clv-locro`)."
            ) from exc

        model_dir_env = os.environ.get(MODEL_DIR_ENV)
        model_dir = Path(model_dir_env) if model_dir_env else None
        logger.info(
            f"locro: loading screen-ai (model_dir={model_dir or 'auto'}, "
            f"light_mode={LIGHT_MODE})"
        )
        _ENGINE = ScreenAI(model_dir=model_dir, light_mode=LIGHT_MODE)
        try:
            major, minor = _ENGINE.version  # type: ignore[attr-defined]
            logger.info(f"locro: ready (version {major}.{minor})")
        except Exception:
            logger.info("locro: ready")
        return _ENGINE


def warm_up() -> None:
    """Preload the screen-ai library so first-page timing is honest.

    Loading the DLL has a side effect: locro calls ``_suppress_native_stderr``
    which permanently redirects fd 2 to /dev/null and reassigns ``sys.stderr``
    to a fresh wrapper around the original fd. Any loguru sink configured
    *before* this point now writes to /dev/null. The caller is responsible
    for re-adding any loguru sinks after this returns (see
    :func:`reattach_loguru_sink`).
    """
    _load()


def reattach_loguru_sink(verbose: bool = False) -> None:
    """Re-attach the loguru stderr sink after locro has redirected fd 2.

    Must be called *after* :func:`warm_up`/:func:`_load`. Drops existing
    sinks and adds a fresh one pointed at the current ``sys.stderr`` (which
    locro has redirected to the original fd-2).
    """
    import sys

    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        level=level,
        colorize=True,
    )


def is_available() -> bool:
    """Return True if the `locro` package can be imported."""
    try:
        import locro  # noqa: F401
    except ImportError:
        return False
    return True


def ocr_image(image: PILImage.Image) -> str:
    """Run locro screen-ai OCR on a single PIL image and return text."""
    engine = _load()
    with _INFER_LOCK:
        page = engine.ocr_pil_image(image)  # type: ignore[attr-defined]
    return page.text


def ocr_pdf_pages(pdf_path: Path, page_indices: Iterable[int]) -> dict[int, str]:
    """Run locro on selected pages of a PDF.

    ``page_indices`` are 0-based; the returned dict is keyed by 0-based index
    to match the rest of the pipeline. locro renders pages itself via PyMuPDF
    at a DPI it picks based on its own max-image-dimension, so callers must
    not pre-render.

    Pages are processed one at a time so callers get per-page progress
    logs. The engine + model stay loaded across calls, so the per-page
    overhead is just one PDF render + one DLL call.
    """
    sorted_idx = sorted(set(page_indices))
    if not sorted_idx:
        return {}
    engine = _load()
    total = len(sorted_idx)
    name = pdf_path.name
    results: dict[int, str] = {}
    for n, idx in enumerate(sorted_idx, start=1):
        with _INFER_LOCK:
            result = engine.ocr(str(pdf_path), pages=[idx + 1])  # type: ignore[attr-defined]
        if result.pages:
            text = result.pages[0].text
        else:
            text = ""
        results[idx] = text
        logger.info(f"locro {name}: page {idx + 1} ({n}/{total}) {len(text.split())}w")
    return results
