# DocumentIndexer

PDF to searchable text. Two tools in one package:

1. **`docindex-ocr`** — CLI that turns PDFs into text. Uses embedded text layers when present, falls back to Tesseract OCR otherwise. Supports per-document language auto-detection. Writes plain text or NDJSON.
2. **`docindex-gui`** — desktop GUI (PySide6) that ingests `.txt` and `.ndjson` outputs into a [Tantivy](https://github.com/quickwit-oss/tantivy) full-text index and provides ranked search with HTML snippets.

## Install

Requires Python 3.10+, [Tesseract](https://github.com/tesseract-ocr/tesseract), and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:polymood/DocumentIndexer.git
cd DocumentIndexer
uv sync
```

System packages (Debian/Ubuntu/Fedora):

```bash
# Debian/Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu

# Fedora
sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-deu
```

Add more `tesseract-ocr-<lang>` packages as needed.

## OCR pipeline

```bash
uv run docindex-ocr INPUT [INPUT ...] --output OUT_DIR [OPTIONS]
```

- `INPUT` — PDF file or directory (recursed for `*.pdf`).
- `--output` — destination directory. Mirrors the input layout under it.
- `--format {txt,ndjson}` — output format. Default `txt`. `ndjson` writes one record per document with a metadata block plus the full text.
- `--ocr-lang LANG` — Tesseract language string (e.g. `eng`, `eng+deu`). Default `eng+deu`.
- `--auto-lang` — per-document language detection. Order of resolution:
  1. parent-folder map (edit `LANG_MAP` in `lang.py`),
  2. lingua detector on any direct-extracted text,
  3. sample-OCR of page 0 with `eng`, then lingua.
- `--ocr-dpi N` — render DPI for OCR. Default 200. Use 300 for small print.
- `--ocr-workers N` — number of pages OCR'd in parallel. Default = CPU count.
- `--max-pages N` — pages rendered per OCR batch. Default 24.
- `--force-ocr` — OCR even when a text layer exists.
- `--workers N` — number of PDFs processed in parallel. Default 1 (recommended: one PDF at a time, all cores on its pages).

### Customising NDJSON metadata

Edit `src/document_indexer/ocr/metadata.py`. The `build_metadata(pdf_path)` function returns a dict that is merged into every NDJSON record. Add fields as needed (publication, year, source URL, etc.).

## Indexer GUI

```bash
uv run docindex-gui [--index DIR] [--ingest PATH] [--format {auto,txt,ndjson}]
```

- `--index` — Tantivy index directory. Default `docindex.tantivy`.
- `--ingest` — file or directory to import before opening the GUI. Use `--format` to override auto-detection. Repeatable.

The GUI provides a search box, BM25-ranked results, HTML snippet preview, and an action to open the source file. Indexing can also be triggered from the GUI via **File → Ingest folder…**.

Query syntax follows Tantivy: `radio AND broadcast`, `"exact phrase"`, `title:history`, `body:tower~`, etc.

## Development

```bash
uv sync --extra dev
uv run pre-commit install
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy
```

## License

MIT. See [LICENSE](LICENSE).
