"""Resume-aware progress tracker.

State lives in a single NDJSON file inside the output directory:
``<output>/.docindex-progress.ndjson``. One line per **fully completed**
input (PDF for plain mode, IA item directory for IA mode). Crashes mid-way
through a file leave no entry, so the next run will redo that file.

Format (one record per line)::

    {"key": "<absolute path>", "method": "...", "pages": 12, "words": 3456,
     "ts": "2026-06-13T15:30:00Z"}

The key is whatever the caller passed (typically an absolute filesystem
path); resume is a simple set-membership check.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path

from loguru import logger

PROGRESS_FILENAME = ".docindex-progress.ndjson"


class ProgressTracker:
    """Append-only NDJSON ledger of completed inputs."""

    def __init__(self, state_path: Path, enabled: bool = True) -> None:
        self.state_path = state_path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._done: set[str] = set()
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with self.state_path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = rec.get("key")
                    if isinstance(key, str):
                        self._done.add(key)
        except OSError as exc:
            logger.warning(f"progress: failed to read {self.state_path}: {exc}")

    @property
    def completed_count(self) -> int:
        return len(self._done)

    def is_done(self, key: str) -> bool:
        if not self.enabled:
            return False
        return key in self._done

    def mark_done(self, key: str, **fields: object) -> None:
        """Append a completion record. Atomic per line on POSIX."""
        if not self.enabled:
            return
        rec = {
            "key": key,
            "ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._lock:
            if key in self._done:
                return
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                # Open + write + fsync so a power loss cannot leave a torn
                # line in the ledger.
                with self.state_path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                self._done.add(key)
            except OSError as exc:
                logger.warning(f"progress: failed to append {self.state_path}: {exc}")

    def reset(self) -> None:
        """Delete the ledger and clear the in-memory set."""
        with self._lock:
            if self.state_path.exists():
                try:
                    self.state_path.unlink()
                except OSError as exc:
                    logger.warning(f"progress: failed to delete {self.state_path}: {exc}")
            self._done.clear()
