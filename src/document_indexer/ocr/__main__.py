"""Allow ``python -m document_indexer.ocr``."""

from document_indexer.ocr.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
