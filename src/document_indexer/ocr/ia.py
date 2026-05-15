"""Internet Archive layout support.

A typical IA item is a directory whose files share a common ``<identifier>``
prefix. The interesting siblings are:

  <identifier>.pdf              -- original PDF
  <identifier>_djvu.txt         -- flat OCR text already produced by IA
  <identifier>_djvu.xml         -- DjVu XML with positions
  <identifier>_hocr.html        -- HTML OCR
  <identifier>_meta.xml         -- IA metadata block (title, creator, ...)

When a ``_djvu.txt`` is available, re-running Tesseract on the PDF is a
waste of compute and storage. The helpers here detect item directories,
expose the relevant file paths, and parse the IA metadata block so the
caller can choose the cheaper path.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

META_FIELDS_SCALAR: tuple[str, ...] = (
    "identifier",
    "title",
    "creator",
    "date",
    "publicdate",
    "addeddate",
    "description",
    "mediatype",
    "language",
    "publisher",
    "subject",
    "scanner",
    "ppi",
    "imagecount",
    "ocr",
    "ocr_parameters",
    "ocr_detected_lang",
    "ocr_detected_script",
    "identifier-access",
    "identifier-ark",
)
META_FIELDS_LIST: tuple[str, ...] = ("collection",)


@dataclass(slots=True)
class IAItem:
    """One Internet Archive item directory."""

    identifier: str
    directory: Path
    pdf_path: Path | None = None
    djvu_txt_path: Path | None = None
    meta_xml_path: Path | None = None
    files_xml_path: Path | None = None
    extras: dict[str, Path] = field(default_factory=dict)

    @property
    def has_djvu_text(self) -> bool:
        return self.djvu_txt_path is not None and self.djvu_txt_path.is_file()

    @property
    def has_pdf(self) -> bool:
        return self.pdf_path is not None and self.pdf_path.is_file()


def _detect_identifier(directory: Path) -> str | None:
    """Pick the IA identifier from filenames in ``directory``.

    Returns the longest common stem-prefix of files that match the
    ``<id>_djvu.txt`` / ``<id>_files.xml`` / ``<id>_meta.xml`` / ``<id>.pdf``
    naming convention, falling back to the directory name when nothing
    matches.
    """
    if not directory.is_dir():
        return None
    candidates: list[str] = []
    for entry in directory.iterdir():
        name = entry.name
        for suffix in ("_djvu.txt", "_files.xml", "_meta.xml", "_djvu.xml"):
            if name.endswith(suffix):
                candidates.append(name[: -len(suffix)])
                break
        else:
            if entry.suffix.lower() == ".pdf":
                candidates.append(entry.stem)
    if not candidates:
        return None
    # Pick the most common candidate; ties broken by length (prefer longest).
    counts: dict[str, int] = {}
    for cand in candidates:
        counts[cand] = counts.get(cand, 0) + 1
    sorted_candidates = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return sorted_candidates[0][0]


def is_ia_item(directory: Path) -> bool:
    """Return True if ``directory`` looks like an Internet Archive item."""
    if not directory.is_dir():
        return False
    identifier = _detect_identifier(directory)
    if identifier is None:
        return False
    markers = (
        f"{identifier}_djvu.txt",
        f"{identifier}_files.xml",
        f"{identifier}_meta.xml",
        f"{identifier}_djvu.xml",
    )
    return any((directory / m).is_file() for m in markers)


def load_ia_item(directory: Path) -> IAItem | None:
    """Build an :class:`IAItem` for ``directory`` or return None."""
    identifier = _detect_identifier(directory)
    if identifier is None:
        return None
    pdf = directory / f"{identifier}.pdf"
    djvu = directory / f"{identifier}_djvu.txt"
    meta = directory / f"{identifier}_meta.xml"
    files = directory / f"{identifier}_files.xml"
    item = IAItem(
        identifier=identifier,
        directory=directory,
        pdf_path=pdf if pdf.is_file() else None,
        djvu_txt_path=djvu if djvu.is_file() else None,
        meta_xml_path=meta if meta.is_file() else None,
        files_xml_path=files if files.is_file() else None,
    )
    if item.pdf_path is None and item.djvu_txt_path is None:
        return None
    return item


def iter_ia_items(root: Path) -> Iterator[IAItem]:
    """Yield every IA item directory found under ``root`` (depth-first)."""
    if not root.is_dir():
        return
    # If the root itself looks like an item, yield it and stop recursion.
    item = load_ia_item(root)
    if item is not None:
        yield item
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        nested_item = load_ia_item(entry)
        if nested_item is not None:
            yield nested_item
            continue
        # Recurse for collections-of-collections.
        yield from iter_ia_items(entry)


def parse_meta_xml(path: Path) -> dict[str, Any]:
    """Extract a flat dict of useful fields from an IA ``_meta.xml`` file."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        logger.warning(f"Could not parse IA meta XML {path}: {exc}")
        return {}
    root = tree.getroot()
    out: dict[str, Any] = {}
    for child in root:
        tag = child.tag
        value = (child.text or "").strip()
        if not value:
            continue
        if tag in META_FIELDS_LIST:
            existing = out.setdefault(tag, [])
            if isinstance(existing, list):
                existing.append(value)
        elif tag in META_FIELDS_SCALAR:
            if tag not in out:
                out[tag] = value
    return out


def read_djvu_text(path: Path) -> str:
    """Read an IA ``_djvu.txt`` file as UTF-8 with lenient decoding."""
    return path.read_text(encoding="utf-8", errors="replace")
