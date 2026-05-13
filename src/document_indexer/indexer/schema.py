"""Schema definitions and value extractors for the indexer.

A :class:`FieldDef` describes one Tantivy field plus the rule that decides where
its value comes from for each document. :class:`IndexerConfig` bundles a list of
fields with the run parameters (paths, mode, heap size, threads).

Source extractors derive values from either a file path (TXT mode and per-file
NDJSON context) or a parsed JSON object (NDJSON mode).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

FieldType = Literal["text", "integer", "date"]
Tokenizer = Literal["default", "raw", "en_stem"]
InputMode = Literal["txt", "ndjson"]


FIELD_TYPES: tuple[FieldType, ...] = ("text", "integer", "date")
TOKENIZERS: tuple[Tokenizer, ...] = ("default", "raw", "en_stem")
INPUT_MODES: tuple[InputMode, ...] = ("txt", "ndjson")

SOURCES: tuple[str, ...] = (
    "file_content",
    "filename",
    "filename_stem",
    "relative_path",
    "parent_dir",
    "full_path",
    "literal",
    "regex_filename",
    "mtime_iso",
    "json_key",
)

SOURCE_HELP: dict[str, str] = {
    "file_content": "TXT mode: full text of the file.",
    "filename": "Basename including extension.",
    "filename_stem": "Basename without extension.",
    "relative_path": "Path relative to the source folder.",
    "parent_dir": "Name of the parent directory.",
    "full_path": "Absolute path on disk.",
    "literal": "Constant value. Put the value in 'source arg'.",
    "regex_filename": "Regex applied to the filename. Source arg = '<regex>||<group>'.",
    "mtime_iso": "File modification date (YYYY-MM-DD).",
    "json_key": (
        "NDJSON mode: pull this key from the JSON object on each line. "
        "Source arg = JSON key (defaults to the field name)."
    ),
}


@dataclass(slots=True)
class FieldDef:
    """One Tantivy schema field plus its data source."""

    name: str = ""
    type: FieldType = "text"
    stored: bool = True
    indexed: bool = True
    fast: bool = False
    tokenizer: Tokenizer = "default"
    source: str = "file_content"
    source_arg: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IndexerConfig:
    """Full indexing job description."""

    src_folder: str = ""
    index_path: str = ""
    glob_pattern: str = "*.txt"
    recursive: bool = True
    input_mode: InputMode = "txt"
    heap_mb: int = 200
    threads: int = 1
    fields: list[FieldDef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fields"] = [f.to_dict() for f in self.fields]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexerConfig:
        fields_data = data.pop("fields", []) or []
        fields = [FieldDef(**f) for f in fields_data]
        return cls(**data, fields=fields)


def extract_from_file(src: str, arg: str, file_path: Path, root: Path) -> Any:
    """Derive a value from a file path / contents (TXT mode and per-file NDJSON)."""
    if src == "file_content":
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if src == "filename":
        return file_path.name
    if src == "filename_stem":
        return file_path.stem
    if src == "relative_path":
        try:
            return str(file_path.relative_to(root))
        except ValueError:
            return str(file_path)
    if src == "parent_dir":
        return file_path.parent.name
    if src == "full_path":
        return str(file_path)
    if src == "literal":
        return arg
    if src == "regex_filename":
        pattern, _, group_str = arg.partition("||")
        try:
            group = int(group_str) if group_str.strip() else 1
        except ValueError:
            return ""
        if not pattern:
            return ""
        match = re.search(pattern, file_path.name)
        if not match:
            return ""
        try:
            return match.group(group)
        except (IndexError, re.error):
            return ""
    if src == "mtime_iso":
        return datetime.fromtimestamp(file_path.stat().st_mtime).date().isoformat()
    return ""


def extract_from_obj(src: str, arg: str, obj: dict[str, Any], fallback_field: str) -> Any:
    """Derive a value from a parsed NDJSON object."""
    if src == "json_key":
        key = arg.strip() or fallback_field
        return obj.get(key)
    if src == "literal":
        return arg
    return None


def coerce(value: Any, type_: FieldType) -> Any:
    """Coerce a raw value to the field's declared type. Returns None on failure."""
    if value is None:
        return None
    if type_ == "integer":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
    if type_ == "date":
        s = str(value).strip()
        if not s:
            return None
        try:
            if len(s) == 10:
                return datetime.fromisoformat(s + "T00:00:00")
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return str(value)


def load_ndjson_schema(path: Path) -> list[FieldDef]:
    """Load a list of :class:`FieldDef` from a newline-delimited JSON schema file."""
    fields: list[FieldDef] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        type_value = str(obj.get("type", "text"))
        tok_value = str(obj.get("tokenizer", "default"))
        fields.append(
            FieldDef(
                name=str(obj.get("name", "")),
                type=type_value if type_value in FIELD_TYPES else "text",
                stored=bool(obj.get("stored", True)),
                indexed=bool(obj.get("indexed", True)),
                fast=bool(obj.get("fast", False)),
                tokenizer=tok_value if tok_value in TOKENIZERS else "default",
                source=str(obj.get("source", "json_key")),
                source_arg=str(obj.get("source_arg", obj.get("name", ""))),
            )
        )
    return fields


def default_txt_fields() -> list[FieldDef]:
    return [
        FieldDef(name="body", type="text", stored=True, source="file_content"),
        FieldDef(name="filename", type="text", stored=True, tokenizer="raw", source="filename"),
        FieldDef(
            name="source_url", type="text", stored=True, tokenizer="raw", source="relative_path"
        ),
    ]


def default_ndjson_fields() -> list[FieldDef]:
    return [
        FieldDef(
            name="body",
            type="text",
            stored=True,
            source="json_key",
            source_arg="body",
        ),
        FieldDef(
            name="source_url",
            type="text",
            stored=True,
            tokenizer="raw",
            source="json_key",
            source_arg="source_url",
        ),
    ]
