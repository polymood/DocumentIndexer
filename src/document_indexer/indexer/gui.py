"""PySide6 desktop GUI for the DocumentIndexer (Tantivy-backed)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from document_indexer import __version__
from document_indexer.indexer.db import Index, SearchHit
from document_indexer.indexer.ingest import Format, ingest


class _IngestWorker(QObject):
    """Background worker that ingests files into the index."""

    finished = Signal(int, int, int)
    failed = Signal(str)

    def __init__(self, index_path: Path, paths: list[Path], format_: Format) -> None:
        super().__init__()
        self._index_path = index_path
        self._paths = paths
        self._format = format_

    def run(self) -> None:
        try:
            index = Index(self._index_path)
            stats = ingest(index, self._paths, self._format)
            index.close()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(stats.files, stats.documents, stats.errors)


class MainWindow(QMainWindow):
    """Top-level window: search bar, results tree, preview pane."""

    def __init__(self, index_path: Path) -> None:
        super().__init__()
        self._index = Index(index_path)
        self._thread: QThread | None = None
        self._worker: _IngestWorker | None = None

        self.setWindowTitle(f"DocumentIndexer {__version__}")
        self.resize(1100, 720)

        self._build_menu()
        self._build_central()
        self._build_status_bar()
        self._refresh_status()

    def _build_menu(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")

        ingest_files_action = QAction("Ingest &files...", self)
        ingest_files_action.triggered.connect(self._action_ingest_files)
        file_menu.addAction(ingest_files_action)

        ingest_folder_action = QAction("Ingest fol&der...", self)
        ingest_folder_action.triggered.connect(self._action_ingest_folder)
        file_menu.addAction(ingest_folder_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = bar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._action_about)
        help_menu.addAction(about_action)

    def _build_central(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            'Tantivy query (e.g. radio AND broadcast, "phrase here", title:history)'
        )
        self._search_input.returnPressed.connect(self._run_search)
        self._search_button = QPushButton("Search")
        self._search_button.clicked.connect(self._run_search)
        search_row.addWidget(self._search_input, stretch=1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self._results = QTreeWidget()
        self._results.setHeaderLabels(["Title", "Source", "Score"])
        self._results.setRootIsDecorated(False)
        self._results.setColumnWidth(0, 340)
        self._results.setColumnWidth(1, 380)
        self._results.itemSelectionChanged.connect(self._on_selection_changed)
        self._results.itemActivated.connect(self._on_item_activated)
        splitter.addWidget(self._results)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Snippet"))
        self._snippet_view = QTextBrowser()
        self._snippet_view.setOpenExternalLinks(False)
        right_layout.addWidget(self._snippet_view, stretch=2)

        self._open_button = QPushButton("Open source file")
        self._open_button.clicked.connect(self._open_selected)
        self._open_button.setEnabled(False)
        right_layout.addWidget(self._open_button)

        right_layout.addWidget(QLabel("Metadata"))
        self._metadata_view = QPlainTextEdit()
        self._metadata_view.setReadOnly(True)
        self._metadata_view.setPlaceholderText("Metadata (JSON)")
        right_layout.addWidget(self._metadata_view, stretch=1)

        splitter.addWidget(right)
        splitter.setSizes([700, 400])
        layout.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self._search_input.setFocus()

    def _build_status_bar(self) -> None:
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)

    def _refresh_status(self) -> None:
        count = self._index.document_count()
        self._status.showMessage(f"Index: {self._index.path}  |  Documents: {count}")

    def _run_search(self) -> None:
        query = self._search_input.text().strip()
        self._results.clear()
        self._snippet_view.clear()
        self._metadata_view.clear()
        self._open_button.setEnabled(False)
        if not query:
            return
        try:
            hits = self._index.search(query, limit=200)
        except Exception as exc:
            QMessageBox.warning(self, "Search failed", str(exc))
            return
        for hit in hits:
            item = QTreeWidgetItem([hit.title, hit.source_path, f"{hit.score:.3f}"])
            item.setData(0, Qt.ItemDataRole.UserRole, hit)
            self._results.addTopLevelItem(item)
        self._status.showMessage(f"{len(hits)} result(s) for: {query}", 5000)

    def _selected_hit(self) -> SearchHit | None:
        items = self._results.selectedItems()
        if not items:
            return None
        hit = items[0].data(0, Qt.ItemDataRole.UserRole)
        return hit if isinstance(hit, SearchHit) else None

    def _on_selection_changed(self) -> None:
        hit = self._selected_hit()
        if hit is None:
            self._open_button.setEnabled(False)
            self._snippet_view.clear()
            self._metadata_view.clear()
            return
        self._open_button.setEnabled(True)
        self._snippet_view.setHtml(hit.snippet or "<i>(no snippet)</i>")
        self._metadata_view.setPlainText(json.dumps(hit.metadata, ensure_ascii=False, indent=2))

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        hit = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(hit, SearchHit):
            self._open_path(Path(hit.source_path))

    def _open_selected(self) -> None:
        hit = self._selected_hit()
        if hit is not None:
            self._open_path(Path(hit.source_path))

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.information(self, "Not found", f"Source file no longer exists:\n{path}")
            return
        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif sys.platform.startswith("win"):
                import os

                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        except OSError as exc:
            QMessageBox.warning(self, "Could not open file", str(exc))

    def _action_ingest_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose .txt or .ndjson files",
            "",
            "Text and NDJSON (*.txt *.ndjson);;All files (*)",
        )
        if files:
            self._start_ingest([Path(p) for p in files])

    def _action_ingest_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to ingest")
        if folder:
            self._start_ingest([Path(folder)])

    def _start_ingest(self, paths: list[Path]) -> None:
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(self, "Busy", "An ingest is already running.")
            return
        self._status.showMessage("Ingesting...")
        self._thread = QThread(self)
        self._worker = _IngestWorker(self._index.path, paths, "auto")
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_ingest_done)
        self._worker.failed.connect(self._on_ingest_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_ingest_done(self, files: int, documents: int, errors: int) -> None:
        self._status.showMessage(
            f"Ingest complete: {files} file(s), {documents} document(s), {errors} error(s).",
            8000,
        )
        QTimer.singleShot(0, self._reopen_index)

    def _reopen_index(self) -> None:
        """Reopen the index so the searcher sees committed segments."""
        path = self._index.path
        self._index.close()
        self._index = Index(path)
        self._refresh_status()

    def _on_ingest_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Ingest failed", message)
        self._refresh_status()

    def _action_about(self) -> None:
        QMessageBox.about(
            self,
            "About DocumentIndexer",
            f"DocumentIndexer {__version__}\n\n"
            "PDF to searchable text. Tantivy-backed desktop search.\n"
            "https://github.com/polymood/DocumentIndexer",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        self._index.close()
        super().closeEvent(event)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docindex-gui",
        description="Desktop search GUI for txt/ndjson documents (Tantivy index).",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("docindex.tantivy"),
        help="Index directory (created on first run).",
    )
    parser.add_argument(
        "--ingest",
        type=Path,
        action="append",
        default=None,
        help="File or directory to ingest before opening the window (repeatable).",
    )
    parser.add_argument(
        "--format",
        choices=["auto", "txt", "ndjson"],
        default="auto",
        help="Format hint for --ingest (default: auto-detect from extension).",
    )
    parser.add_argument("--version", action="version", version=f"docindex-gui {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``docindex-gui`` script."""
    args = _build_parser().parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")

    if args.ingest:
        index = Index(args.index)
        stats = ingest(index, args.ingest, args.format)
        index.close()
        logger.info(
            f"Pre-ingest: {stats.files} file(s), {stats.documents} document(s), "
            f"{stats.errors} error(s)."
        )

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(args.index)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
