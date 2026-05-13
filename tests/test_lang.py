"""Tests for the language-resolution helpers."""

from __future__ import annotations

from pathlib import Path

from document_indexer.ocr.lang import lang_from_path


def test_path_map_german() -> None:
    assert lang_from_path(Path("/data/Berliner-Zeitung/1987/SNP.pdf")) == "deu"
    assert lang_from_path(Path("/data/Funkschau/1962/issue.pdf")) == "deu"


def test_path_map_french() -> None:
    assert lang_from_path(Path("/data/Le-Haut-Parleur/1955/april.pdf")) == "fra"


def test_path_map_miss() -> None:
    assert lang_from_path(Path("/data/Billboard/1972/issue.pdf")) is None
