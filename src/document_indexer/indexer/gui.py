"""PySide6 GUI for building Tantivy indexes from TXT or NDJSON folders."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from document_indexer import __version__
from document_indexer.indexer.builder import IndexResult, run
from document_indexer.indexer.schema import (
    FIELD_TYPES,
    SOURCE_HELP,
    SOURCES,
    TOKENIZERS,
    FieldDef,
    IndexerConfig,
    InputMode,
    default_ndjson_fields,
    default_txt_fields,
    load_ndjson_schema,
)

COL_NAME = 0
COL_TYPE = 1
COL_STORED = 2
COL_INDEXED = 3
COL_FAST = 4
COL_TOKENIZER = 5
COL_SOURCE = 6
COL_SOURCE_ARG = 7
N_COLS = 8

COL_TOOLTIPS: dict[int, str] = {
    COL_NAME: "Tantivy field name. Used in the index schema and as JSON key (NDJSON mode default).",
    COL_TYPE: "text - full-text searchable. integer - numeric range/sort. date - datetime.",
    COL_STORED: "Keep the value retrievable. Required for snippets and metadata display.",
    COL_INDEXED: "Make the field indexed (only meaningful for integer/date; text is always indexed).",
    COL_FAST: "Add to fast columns - needed for sorting/filtering on integer/date.",
    COL_TOKENIZER: "default - lowercase + word split. raw - exact token. en_stem - English stemmer.",
    COL_SOURCE: "Where to pull the value from. Hover the source-arg cell for per-source hints.",
    COL_SOURCE_ARG: (
        "Argument for the source. literal: value. regex_filename: '<regex>||<group>'. "
        "json_key: JSON key (blank = field name)."
    ),
}

HEAP_PRESETS_MB: tuple[int, ...] = (200, 500, 1000, 2000, 4000)


def _row(parent: QWidget, widgets: list[QWidget]) -> None:
    layout = QHBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)


class _CenteredCheckBox(QWidget):
    """A QCheckBox centered in a cell with an accessible ``checkbox`` attribute."""

    def __init__(self, checked: bool, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.setToolTip(tooltip)
        layout.addWidget(self.checkbox, alignment=Qt.AlignmentFlag.AlignCenter)


class FieldTable(QTableWidget):
    """Editable table of :class:`FieldDef` rows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, N_COLS, parent)
        headers = [
            "name",
            "type",
            "stored",
            "indexed",
            "fast",
            "tokenizer",
            "source",
            "source arg",
        ]
        self.setHorizontalHeaderLabels(headers)
        for col, tip in COL_TOOLTIPS.items():
            item = self.horizontalHeaderItem(col)
            if item is not None:
                item.setToolTip(tip)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

    def add_field(self, fd: FieldDef | None = None) -> None:
        fd = fd or FieldDef()
        row = self.rowCount()
        self.insertRow(row)

        name_edit = QLineEdit(fd.name)
        name_edit.setToolTip(COL_TOOLTIPS[COL_NAME])
        self.setCellWidget(row, COL_NAME, name_edit)

        type_combo = QComboBox()
        type_combo.addItems(FIELD_TYPES)
        type_combo.setCurrentText(fd.type)
        type_combo.setToolTip(COL_TOOLTIPS[COL_TYPE])
        self.setCellWidget(row, COL_TYPE, type_combo)

        self.setCellWidget(row, COL_STORED, _CenteredCheckBox(fd.stored, COL_TOOLTIPS[COL_STORED]))
        self.setCellWidget(
            row, COL_INDEXED, _CenteredCheckBox(fd.indexed, COL_TOOLTIPS[COL_INDEXED])
        )
        self.setCellWidget(row, COL_FAST, _CenteredCheckBox(fd.fast, COL_TOOLTIPS[COL_FAST]))

        tok_combo = QComboBox()
        tok_combo.addItems(TOKENIZERS)
        tok_combo.setCurrentText(fd.tokenizer)
        tok_combo.setToolTip(COL_TOOLTIPS[COL_TOKENIZER])
        self.setCellWidget(row, COL_TOKENIZER, tok_combo)

        src_combo = QComboBox()
        src_combo.addItems(SOURCES)
        src_combo.setCurrentText(fd.source)
        src_combo.setToolTip(COL_TOOLTIPS[COL_SOURCE])

        arg_edit = QLineEdit(fd.source_arg)
        help_text = SOURCE_HELP.get(fd.source, "")
        arg_edit.setPlaceholderText(help_text)
        arg_edit.setToolTip(help_text)

        def _on_source_changed(text: str, edit: QLineEdit = arg_edit) -> None:
            tip = SOURCE_HELP.get(text, "")
            edit.setPlaceholderText(tip)
            edit.setToolTip(tip)

        src_combo.currentTextChanged.connect(_on_source_changed)

        self.setCellWidget(row, COL_SOURCE, src_combo)
        self.setCellWidget(row, COL_SOURCE_ARG, arg_edit)

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        for row in rows:
            self.removeRow(row)

    def collect(self) -> list[FieldDef]:
        result: list[FieldDef] = []
        for row in range(self.rowCount()):
            name_widget = self.cellWidget(row, COL_NAME)
            assert isinstance(name_widget, QLineEdit)
            name = name_widget.text().strip()
            if not name:
                continue
            type_widget = self.cellWidget(row, COL_TYPE)
            stored_widget = self.cellWidget(row, COL_STORED)
            indexed_widget = self.cellWidget(row, COL_INDEXED)
            fast_widget = self.cellWidget(row, COL_FAST)
            tok_widget = self.cellWidget(row, COL_TOKENIZER)
            src_widget = self.cellWidget(row, COL_SOURCE)
            arg_widget = self.cellWidget(row, COL_SOURCE_ARG)
            assert isinstance(type_widget, QComboBox)
            assert isinstance(stored_widget, _CenteredCheckBox)
            assert isinstance(indexed_widget, _CenteredCheckBox)
            assert isinstance(fast_widget, _CenteredCheckBox)
            assert isinstance(tok_widget, QComboBox)
            assert isinstance(src_widget, QComboBox)
            assert isinstance(arg_widget, QLineEdit)
            result.append(
                FieldDef(
                    name=name,
                    type=type_widget.currentText(),  # type: ignore[arg-type]
                    stored=stored_widget.checkbox.isChecked(),
                    indexed=indexed_widget.checkbox.isChecked(),
                    fast=fast_widget.checkbox.isChecked(),
                    tokenizer=tok_widget.currentText(),  # type: ignore[arg-type]
                    source=src_widget.currentText(),
                    source_arg=arg_widget.text(),
                )
            )
        return result

    def load(self, fields: list[FieldDef]) -> None:
        self.setRowCount(0)
        for fd in fields:
            self.add_field(fd)


class HeapSelector(QWidget):
    """Preset buttons + custom spinbox for Tantivy writer heap size."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for mb in HEAP_PRESETS_MB:
            btn = QPushButton(f"{mb} MB" if mb < 1000 else f"{mb // 1000} GB")
            btn.setCheckable(True)
            btn.setToolTip(
                f"Tantivy writer heap. Bigger = faster bulk index but more RAM. {mb} MB."
            )
            btn.clicked.connect(lambda _checked=False, val=mb: self._on_preset(val))
            layout.addWidget(btn)
            self._group.addButton(btn)
            btn.setProperty("mb", mb)

        self._custom_spin = QSpinBox()
        self._custom_spin.setRange(20, 16384)
        self._custom_spin.setSuffix(" MB")
        self._custom_spin.setToolTip("Custom heap size (MB).")
        self._custom_spin.valueChanged.connect(self._on_custom_changed)
        layout.addWidget(QLabel("custom:"))
        layout.addWidget(self._custom_spin)

        self._value_mb = 200
        self._select_preset(200)

    def _on_preset(self, mb: int) -> None:
        self._value_mb = mb
        self._custom_spin.blockSignals(True)
        self._custom_spin.setValue(mb)
        self._custom_spin.blockSignals(False)

    def _on_custom_changed(self, mb: int) -> None:
        self._value_mb = mb
        for btn in self._group.buttons():
            btn.setChecked(btn.property("mb") == mb)

    def _select_preset(self, mb: int) -> None:
        for btn in self._group.buttons():
            if btn.property("mb") == mb:
                btn.setChecked(True)
                self._on_preset(mb)
                return
        self._custom_spin.setValue(mb)

    def value_mb(self) -> int:
        return int(self._value_mb)

    def set_value_mb(self, mb: int) -> None:
        self._select_preset(mb)


class IndexerWorker(QObject):
    """Background indexing worker driven by Qt signals."""

    progress = Signal(int, int, str)
    log_line = Signal(str)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, config: IndexerConfig) -> None:
        super().__init__()
        self._config = config
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def _emit_progress(self, done: int, total: int, message: str) -> None:
        self.progress.emit(done, total, message)

    def _emit_log(self, line: str) -> None:
        self.log_line.emit(line)

    def _is_cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:
        try:
            result: IndexResult = run(
                self._config,
                progress=self._emit_progress,
                log=self._emit_log,
                cancelled=self._is_cancelled,
            )
        except Exception:
            self.failed.emit(traceback.format_exc())
            return
        if result.cancelled:
            self.failed.emit("cancelled")
        else:
            self.finished_ok.emit(result.documents)


class MainWindow(QMainWindow):
    """Top-level window: paths + schema editor + run controls."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"DocumentIndexer {__version__}")
        self.resize(1200, 760)

        self._worker: IndexerWorker | None = None
        self._thread: QThread | None = None

        self._build_ui()
        self._add_default_fields_txt()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        toolbar = QToolBar(self)
        self.addToolBar(toolbar)
        load_action = QAction("Load preset", self)
        save_action = QAction("Save preset", self)
        load_action.setToolTip("Load paths + params + schema from JSON.")
        save_action.setToolTip("Save current settings to JSON.")
        load_action.triggered.connect(self._load_preset)
        save_action.triggered.connect(self._save_preset)
        toolbar.addAction(load_action)
        toolbar.addAction(save_action)

        paths_group = QGroupBox("Paths")
        paths_form = QFormLayout(paths_group)

        self._src_edit = QLineEdit()
        self._src_edit.setToolTip(
            "Folder to index. For NDJSON mode, this folder may contain a 'schema.ndjson' "
            "to auto-load the schema."
        )
        self._src_edit.editingFinished.connect(self._maybe_autodetect_ndjson)
        src_btn = QPushButton("Browse...")
        src_btn.clicked.connect(self._pick_src)
        src_wrap = QWidget()
        _row(src_wrap, [self._src_edit, src_btn])
        paths_form.addRow("Source folder", src_wrap)

        self._out_edit = QLineEdit()
        self._out_edit.setToolTip("Where to create the Tantivy index directory.")
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(self._pick_out)
        out_wrap = QWidget()
        _row(out_wrap, [self._out_edit, out_btn])
        paths_form.addRow("Output index", out_wrap)

        self._glob_edit = QLineEdit("*.txt")
        self._glob_edit.setToolTip("Glob pattern for input files (e.g. *.txt, *.ndjson).")
        paths_form.addRow("File pattern", self._glob_edit)

        self._recursive_cb = QCheckBox("Recursive")
        self._recursive_cb.setChecked(True)
        self._recursive_cb.setToolTip("Walk subdirectories of the source folder.")
        paths_form.addRow("", self._recursive_cb)

        self._mode_txt = QRadioButton("TXT (one file = one doc)")
        self._mode_ndjson = QRadioButton("NDJSON (one line = one doc)")
        self._mode_txt.setChecked(True)
        self._mode_txt.setToolTip("Each file's contents become the 'body' field.")
        self._mode_ndjson.setToolTip(
            "Files are NDJSON; each non-empty line is parsed as a JSON object. "
            "Drop a 'schema.ndjson' in the source folder to auto-load the schema."
        )
        self._mode_txt.toggled.connect(self._on_mode_changed)
        mode_wrap = QWidget()
        _row(mode_wrap, [self._mode_txt, self._mode_ndjson])
        paths_form.addRow("Input mode", mode_wrap)

        params_group = QGroupBox("Indexer params")
        params_form = QFormLayout(params_group)
        self._heap = HeapSelector()
        params_form.addRow("Heap size", self._heap)
        self._threads_spin = QSpinBox()
        self._threads_spin.setRange(1, 16)
        self._threads_spin.setValue(1)
        self._threads_spin.setToolTip("Tantivy writer threads.")
        params_form.addRow("Writer threads", self._threads_spin)

        top_row = QHBoxLayout()
        top_row.addWidget(paths_group, 2)
        top_row.addWidget(params_group, 1)
        root.addLayout(top_row)

        schema_group = QGroupBox("Schema (fields)")
        schema_layout = QVBoxLayout(schema_group)
        self._field_table = FieldTable()
        schema_layout.addWidget(self._field_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add field")
        add_btn.clicked.connect(lambda: self._field_table.add_field())
        del_btn = QPushButton("- Remove selected")
        del_btn.clicked.connect(self._field_table.remove_selected)
        defaults_txt_btn = QPushButton("TXT defaults")
        defaults_txt_btn.clicked.connect(self._add_default_fields_txt)
        defaults_nd_btn = QPushButton("NDJSON defaults")
        defaults_nd_btn.clicked.connect(self._add_default_fields_ndjson)
        for btn in (add_btn, del_btn, defaults_txt_btn, defaults_nd_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch()
        schema_layout.addLayout(btn_row)
        root.addWidget(schema_group, 2)

        run_group = QGroupBox("Run")
        run_layout = QVBoxLayout(run_group)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        run_layout.addWidget(self._progress)
        self._status_label = QLabel("idle")
        run_layout.addWidget(self._status_label)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: monospace;")
        run_layout.addWidget(self._log, 1)

        run_btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start indexing")
        self._start_btn.clicked.connect(self._start)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setEnabled(False)
        run_btn_row.addWidget(self._start_btn)
        run_btn_row.addWidget(self._cancel_btn)
        run_btn_row.addStretch()
        run_layout.addLayout(run_btn_row)

        root.addWidget(run_group, 2)

    def _on_mode_changed(self, _checked: bool = False) -> None:
        if self._mode_ndjson.isChecked() and self._glob_edit.text().strip() in ("", "*.txt"):
            self._glob_edit.setText("*.ndjson")
        elif self._mode_txt.isChecked() and self._glob_edit.text().strip() in ("", "*.ndjson"):
            self._glob_edit.setText("*.txt")

    def _maybe_autodetect_ndjson(self) -> None:
        path = Path(self._src_edit.text().strip())
        if not path.is_dir():
            return
        schema_file = path / "schema.ndjson"
        if not schema_file.is_file():
            return
        try:
            fields = load_ndjson_schema(schema_file)
        except Exception as exc:
            QMessageBox.warning(self, "schema.ndjson", f"Could not parse: {exc}")
            return
        if not fields:
            return
        self._mode_ndjson.setChecked(True)
        self._glob_edit.setText("*.ndjson")
        self._field_table.load(fields)
        self._append_log(f"Auto-loaded schema from {schema_file} - {len(fields)} fields.")

    def _add_default_fields_txt(self) -> None:
        self._field_table.load(default_txt_fields())

    def _add_default_fields_ndjson(self) -> None:
        self._field_table.load(default_ndjson_fields())

    def _pick_src(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pick source folder", self._src_edit.text())
        if folder:
            self._src_edit.setText(folder)
            self._maybe_autodetect_ndjson()

    def _pick_out(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Pick output folder for index", self._out_edit.text()
        )
        if folder:
            self._out_edit.setText(folder)

    def _build_config(self) -> IndexerConfig:
        mode: InputMode = "ndjson" if self._mode_ndjson.isChecked() else "txt"
        pattern = self._glob_edit.text().strip() or ("*.ndjson" if mode == "ndjson" else "*.txt")
        return IndexerConfig(
            src_folder=self._src_edit.text().strip(),
            index_path=self._out_edit.text().strip(),
            glob_pattern=pattern,
            recursive=self._recursive_cb.isChecked(),
            input_mode=mode,
            heap_mb=self._heap.value_mb(),
            threads=self._threads_spin.value(),
            fields=self._field_table.collect(),
        )

    def apply_config(self, config: IndexerConfig) -> None:
        self._src_edit.setText(config.src_folder)
        self._out_edit.setText(config.index_path)
        self._glob_edit.setText(config.glob_pattern)
        self._recursive_cb.setChecked(config.recursive)
        if config.input_mode == "ndjson":
            self._mode_ndjson.setChecked(True)
        else:
            self._mode_txt.setChecked(True)
        self._heap.set_value_mb(config.heap_mb)
        self._threads_spin.setValue(config.threads)
        self._field_table.load(config.fields)

    def _save_preset(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save preset", "", "JSON (*.json)")
        if not path:
            return
        Path(path).write_text(json.dumps(self._build_config().to_dict(), indent=2))
        self._append_log(f"Saved preset -> {path}")

    def _load_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load preset", "", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            config = IndexerConfig.from_dict(data)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return
        self.apply_config(config)
        self._append_log(f"Loaded preset <- {path}")

    def _start(self) -> None:
        config = self._build_config()
        try:
            names = [f.name for f in config.fields if f.name]
            if not config.src_folder:
                raise ValueError("Pick a source folder.")
            if not config.index_path:
                raise ValueError("Pick an output index folder.")
            if not names:
                raise ValueError("Define at least one field.")
            if len(set(names)) != len(names):
                raise ValueError("Field names must be unique.")
        except ValueError as exc:
            QMessageBox.warning(self, "Missing", str(exc))
            return

        self._log.clear()
        self._progress.setValue(0)
        self._status_label.setText("starting...")
        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        self._thread = QThread(self)
        self._worker = IndexerWorker(config)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.log_line.connect(self._append_log)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status_label.setText("cancelling...")

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self._progress.setValue(int((done / total) * 100))
        self._status_label.setText(f"{done} / {total}  ·  {message}")

    def _on_done(self, count: int) -> None:
        self._status_label.setText(f"done - {count} documents indexed")
        self._progress.setValue(100)
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._worker = None
        self._append_log(f"\nIndexed {count} documents.")

    def _on_fail(self, message: str) -> None:
        if message.strip() == "cancelled":
            self._status_label.setText("cancelled")
        else:
            self._status_label.setText("failed")
            QMessageBox.critical(self, "Indexing failed", message)
            self._append_log("\nFAILED:\n" + message)
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._worker = None

    def _append_log(self, line: str) -> None:
        self._log.append(line)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docindex-gui",
        description="Tantivy indexer GUI for TXT and NDJSON folders.",
    )
    parser.add_argument("--preset", type=Path, help="Preset JSON to load on startup.")
    parser.add_argument("--version", action="version", version=f"docindex-gui {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``docindex-gui`` script."""
    args = _build_parser().parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    if args.preset is not None:
        try:
            data = json.loads(args.preset.read_text())
            window.apply_config(IndexerConfig.from_dict(data))
        except Exception as exc:
            QMessageBox.critical(window, "Preset load failed", str(exc))
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
