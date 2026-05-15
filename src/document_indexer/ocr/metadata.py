"""User-editable metadata template for NDJSON output.

Modify ``build_metadata`` to attach arbitrary fields to each document record.
The returned mapping is merged into the top-level NDJSON object alongside the
extracted text and the engine-provided statistics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_metadata(
    pdf_path: Path,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-document metadata for NDJSON output.

    Called once per source document before text extraction. Customise freely;
    the result is merged into the NDJSON record without further validation.
    ``extra`` is an optional dict of caller-supplied fields (e.g. Internet
    Archive ``_meta.xml`` contents) that override anything derived here.
    """
    base: dict[str, Any] = {
        "filename": pdf_path.name,
        "stem": pdf_path.stem,
        "source_path": str(pdf_path.resolve()),
        "parent": pdf_path.parent.name,
    }
    if extra:
        base.update(extra)
    return base
