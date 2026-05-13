"""Per-document language resolution for Tesseract OCR."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from lingua import LanguageDetector


LANG_MAP: dict[str, str] = {
    "berliner-zeitung": "deu",
    "funkschau": "deu",
    "funk-technik": "deu",
    "funk-bastler": "deu",
    "radio-mentor": "deu",
    "der-rundfunk": "deu",
    "disques": "fra",
    "haut-parleur": "fra",
    "toute-la-radio": "fra",
    "ondas": "spa",
    "radiocorriere": "ita",
    "radio-bulletin": "nld",
}


_LINGUA_TO_TESS: dict[str, str] = {
    "ENGLISH": "eng",
    "GERMAN": "deu",
    "FRENCH": "fra",
    "SPANISH": "spa",
    "ITALIAN": "ita",
    "DUTCH": "nld",
    "PORTUGUESE": "por",
}


_detector: LanguageDetector | None = None
_detector_unavailable: bool = False


def _get_detector() -> LanguageDetector | None:
    """Return a lingua detector restricted to the supported languages.

    Returns ``None`` if ``lingua-language-detector`` is not installed.
    """
    global _detector, _detector_unavailable
    if _detector is not None or _detector_unavailable:
        return _detector
    try:
        from lingua import Language, LanguageDetectorBuilder

        _detector = LanguageDetectorBuilder.from_languages(
            Language.ENGLISH,
            Language.GERMAN,
            Language.FRENCH,
            Language.SPANISH,
            Language.ITALIAN,
            Language.DUTCH,
            Language.PORTUGUESE,
        ).build()
    except ImportError:
        logger.warning(
            "lingua-language-detector not installed; "
            "auto-lang falls back to the folder map and the default language."
        )
        _detector_unavailable = True
    return _detector


def lang_from_path(pdf_path: Path) -> str | None:
    """Return a Tesseract language code if any path segment matches ``LANG_MAP``."""
    needle = str(pdf_path).lower().replace(" ", "-").replace("_", "-")
    for key, lang in LANG_MAP.items():
        if key in needle:
            return lang
    return None


def detect_lang_from_text(text: str) -> str | None:
    """Detect language with lingua; return a Tesseract code or None."""
    if not text or len(text.strip()) < 100:
        return None
    detector = _get_detector()
    if detector is None:
        return None
    try:
        language = detector.detect_language_of(text[:2000])
    except Exception as exc:
        logger.debug(f"lingua failed: {exc}")
        return None
    if language is None:
        return None
    return _LINGUA_TO_TESS.get(language.name)


def warm_up_detector() -> bool:
    """Eagerly initialise lingua so the model loads once before parallel work."""
    return _get_detector() is not None
