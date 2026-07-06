"""Dictation history store (M10): %LOCALAPPDATA%\\LocalFlow\\history.jsonl.

One JSON object per pasted utterance. Fully local, same privacy stance as
everything else; history_enabled=false means NOTHING is ever written. The
store exists because LocalFlow cannot see edits made in the target app
after pasting - corrections happen in the settings History panel, and each
saved correction feeds the dictionary miner.

Capped at MAX_ENTRIES: on overflow the file is rewritten with the newest
entries (rewrite-on-cap - simple and crash-safe enough for 200 lines).
Thread-safe via one lock: the worker appends, the settings UI edits.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("localflow.desktop.history")

HISTORY_FILENAME = "history.jsonl"
MAX_ENTRIES = 200


def default_history_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LocalFlow"
    return base / HISTORY_FILENAME


class HistoryStore:
    """Append/edit/clear over the jsonl file. Every method is best-effort:
    a history hiccup must never take a dictation down (the caller already
    pasted; this is bookkeeping)."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_history_path()
        self._lock = threading.Lock()

    def append(self, raw_text: str, clean_text: str | None, final_text: str,
               device: str, stt_ms: int) -> None:
        entry = {
            "ts": time.time(),
            "raw_text": raw_text,
            "clean_text": clean_text,
            "final_text": final_text,
            "corrected_text": None,
            "device": device,
            "stt_ms": int(stt_ms),
        }
        try:
            with self._lock:
                entries = self._load_unlocked()
                entries.append(entry)
                if len(entries) > MAX_ENTRIES:
                    entries = entries[-MAX_ENTRIES:]
                    self._write_unlocked(entries)   # rewrite-on-cap
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("history append failed — dictation unaffected")

    def load(self) -> list[dict]:
        """Oldest first. Corrupt lines are skipped, not fatal."""
        with self._lock:
            return self._load_unlocked()

    def set_correction(self, ts: float, corrected_text: str) -> bool:
        """Attach Jeremy's corrected text to the entry with timestamp `ts`
        (the jsonl's stable id). Returns False if the entry is gone."""
        try:
            with self._lock:
                entries = self._load_unlocked()
                for entry in entries:
                    if abs(entry.get("ts", 0) - ts) < 1e-6:
                        entry["corrected_text"] = corrected_text
                        self._write_unlocked(entries)
                        return True
            return False
        except Exception:
            log.exception("history correction save failed")
            return False

    def corrections(self) -> list[tuple[str, str]]:
        """(final_text, corrected_text) pairs for the miner."""
        return [(e["final_text"], e["corrected_text"])
                for e in self.load()
                if e.get("corrected_text") and e.get("final_text")]

    def clear(self) -> None:
        try:
            with self._lock:
                self.path.unlink(missing_ok=True)
            log.info("history cleared (%s)", self.path)
        except Exception:
            log.exception("history clear failed")

    # -- internals (call with the lock held) --------------------------------

    def _load_unlocked(self) -> list[dict]:
        if not self.path.is_file():
            return []
        entries: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.debug("skipping corrupt history line")
        except Exception:
            log.exception("history unreadable — treating as empty")
        return entries

    def _write_unlocked(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
