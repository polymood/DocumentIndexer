"""Tesseract OCR helpers."""

from __future__ import annotations

import os
import subprocess
from multiprocessing import cpu_count

from loguru import logger

os.environ.setdefault("OMP_THREAD_LIMIT", "1")


MAX_OCR_WORKERS: int = max(1, cpu_count())


def clamp_workers(workers: int) -> int:
    """Clamp the requested worker count to ``[1, MAX_OCR_WORKERS]``."""
    return max(1, min(workers, MAX_OCR_WORKERS))


def installed_languages() -> list[str]:
    """Return the list of installed Tesseract language packs."""
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(f"Could not query Tesseract languages: {exc}. Falling back to 'eng'.")
        return ["eng"]

    lines = (result.stderr + result.stdout).splitlines()
    langs = sorted(
        line.strip()
        for line in lines
        if line.strip() and not line.strip().lower().startswith(("list", "error"))
    )
    return langs or ["eng"]


def resolve_lang(lang_arg: str) -> str:
    """Resolve ``auto`` to ``a+b+c`` of every installed pack; pass through otherwise."""
    if lang_arg == "auto":
        langs = installed_languages()
        combined = "+".join(langs)
        logger.info(f"Tesseract languages (auto-detected {len(langs)}): {combined}")
        return combined
    return lang_arg
