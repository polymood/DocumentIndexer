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
- `--ocr-backend {tesseract,deepseek}` — OCR engine. Default `tesseract`. See [DeepSeek-OCR backend](#deepseek-ocr-backend) for the VLM option.
- `--ocr-lang LANG` — Tesseract language string (e.g. `eng`, `eng+deu`). Default `eng+deu`. Ignored when `--ocr-backend deepseek`.
- `--auto-lang` — per-document language detection. Order of resolution:
  1. parent-folder map (edit `LANG_MAP` in `lang.py`),
  2. lingua detector on any direct-extracted text,
  3. sample-OCR of page 0 with `eng`, then lingua.
- `--ocr-dpi N` — render DPI for OCR. Default 200. Use 300 for small print.
- `--ocr-workers N` — number of pages OCR'd in parallel. Default = CPU count.
- `--max-pages N` — pages rendered per OCR batch. Default 24.
- `--force-ocr` — OCR even when a text layer exists.
- `--workers N` — number of PDFs processed in parallel. Default 1 (recommended: one PDF at a time, all cores on its pages).

### DeepSeek-OCR backend

[DeepSeek-OCR](https://huggingface.co/deepseek-ai/DeepSeek-OCR) is a 3B-parameter vision-language model that outputs structured markdown (tables, formulas, headings). Better than Tesseract on complex layouts, scanned books, charts, and CJK text. Requires a GPU.

**1. Install GPU-enabled PyTorch first**, matching your hardware:

```bash
# AMD ROCm 6.2 (RDNA3 like RX 7700/7800/7900 XT/XTX — natively supported)
uv pip install torch --index-url https://download.pytorch.org/whl/rocm6.2

# AMD ROCm 6.3
uv pip install torch --index-url https://download.pytorch.org/whl/rocm6.3

# NVIDIA CUDA 12.4
uv pip install torch --index-url https://download.pytorch.org/whl/cu124
```

**2. Install the extras** (skips torch since it's already there):

```bash
uv pip install -e '.[deepseek]'
```

**3. Run**:

```bash
docindex-ocr book.pdf --output out --ocr-backend deepseek --format ndjson
```

**Environment variables**:

- `DEEPSEEK_OCR_MODEL` — override model id (default `deepseek-ai/DeepSeek-OCR`).
- `DEEPSEEK_OCR_ATTN` — attention impl. Default `eager` (works on ROCm). Set to `flash_attention_2` on NVIDIA after `pip install flash-attn==2.7.3`.
- `DEEPSEEK_OCR_DEVICE` — default `cuda` (ROCm's PyTorch also uses the `cuda` device name via HIP).

**Notes**:

- First run downloads ~6 GB of weights into the HuggingFace cache.
- Uses bf16 + the `gundam` preset (1024 base / 640 image / crop on) by default — best quality/speed tradeoff and fits comfortably in 16 GB VRAM.
- `--ocr-workers` is forced to 1 (the GPU is the bottleneck).
- Output is markdown, not plain text — well-suited for downstream LLM ingestion.

### Customising NDJSON metadata

Edit `src/document_indexer/ocr/metadata.py`. The `build_metadata(pdf_path)` function returns a dict that is merged into every NDJSON record. Add fields as needed (publication, year, source URL, etc.).

## Indexer GUI

```bash
uv run docindex-gui [--preset PRESET.json]
```

The window is a schema-driven Tantivy indexer. It is **not** a search tool — it
only writes the index directory. Pair it with a separate search front-end of
your choice (e.g. Quickwit, a CLI script, or your own UI).

Workflow:

1. **Paths** — pick a source folder and an output directory for the Tantivy index. Choose **TXT** (one file = one document) or **NDJSON** (one line = one document).
2. **Schema** — edit the field table. Each row defines one Tantivy field (`text`/`integer`/`date`) plus the rule that pulls its value (from file contents, filename, regex on the filename, a JSON key, etc.). The `TXT defaults` and `NDJSON defaults` buttons seed sensible defaults.
3. **Indexer params** — Tantivy writer heap size (preset buttons + custom MB) and writer thread count.
4. **Run** — progress bar, log, and a cancel button (rolls back uncommitted writes).

Drop a `schema.ndjson` file at the root of an NDJSON source folder to auto-load the schema. Each non-empty, non-comment line is one field definition.

Presets (paths + params + schema) can be saved/loaded from the toolbar or passed via `--preset PATH.json` on launch.

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
