"""User-editable metadata template for NDJSON output.

Modify ``build_metadata`` to attach arbitrary fields to each document record.
The returned mapping is merged into the top-level NDJSON object alongside the
extracted text and the engine-provided statistics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_metadata(pdf_path: Path) -> dict[str, Any]:
    """Return per-document metadata for NDJSON output.

    Called once per PDF before text extraction. Customise freely; the result is
    merged into the record without further validation.
    """
    return {
        "filename": pdf_path.name,
        "stem": pdf_path.stem,
        "source_path": str(pdf_path.resolve()),
        "parent": pdf_path.parent.name,
    }
