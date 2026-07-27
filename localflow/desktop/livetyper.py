"""M12: the streaming commit engine (log-only).

While a dictation runs, this thread re-decodes the recorder's uncommitted
buffer about once a second and commits the words that two consecutive decodes
agree on (LocalAgreement-2, `core.live.commit_prefix`). Each committed chunk
goes to `on_commit`, which M13 wires to the keystroke injector - so the
committed stream IS what is already in the focused app, and the worker's final
patch (`core.live.patch_plan` against `typed_for`) only has to fix the
difference. The stream is strictly append-only for that reason.

Threading: same discipline as the Segmenter - this thread only READS the
recorder (snapshot) and never queues work. The engine is strictly
single-threaded, so a preview decode takes the app's engine lock
NON-BLOCKING and skips the beat whenever a segment or the final decode owns
it. A preview must never make a real dictation wait behind it.

Preview decodes are the FAST path: no timestamps, no hallucination filter, no
preprocessing. Those belong to the final pass, which is what determines the
text that actually stays (M13 patches the difference); paying denoise on a
growing window every second would eat the cadence this milestone exists to
prove.
"""

from __future__ import annotations

import logging
import threading
import time

from localflow.core import ensure_16k_mono
from localflow.core.live import commit_prefix

log = logging.getLogger("localflow.desktop.livetyper")

POLL_INTERVAL_S = 1.0
MIN_WINDOW_S = 1.5      # a shorter window decodes to noise; wait for audio


class LiveTyper:
    """Polls a StreamRecorder while it records and commits stabilised words.

    `is_dictating() -> bool` and `current_id() -> int` are supplied by the app
    (a mic test also puts the recorder in `recording`, and that audio is not a
    dictation); `engine` is set after warmup, like the Segmenter's VAD.
    """

    def __init__(self, recorder, engine_lock, is_dictating, current_id,
                 engine=None, poll_interval_s: float = POLL_INTERVAL_S,
                 min_window_s: float = MIN_WINDOW_S, on_commit=None):
        self.recorder = recorder
        self.engine = engine
        self.engine_lock = engine_lock
        self.is_dictating = is_dictating
        self.current_id = current_id
        self.poll_interval_s = poll_interval_s
        self.min_window_s = min_window_s
        # M13: called with each newly committed chunk (leading space included
        # when it continues an existing stream). Always called under _lock, so
        # what the app types is exactly what text() says, in that order.
        self.on_commit = on_commit
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="localflow-livetyper")
        # Committed words. `_regions` are windows the Tier 1 segmenter has
        # already cut away (oldest first), `_window` is the live one; the
        # stream is their concatenation. Splitting it that way is what lets a
        # cut segment's authoritative text land AFTER the new window has
        # already committed words without reordering anything.
        self._lock = threading.Lock()
        self._id: int | None = None
        self._regions: list[list[str]] = []
        self._reconciled = 0          # regions still awaiting their segment text
        self._window: list[str] = []
        self._prev: list[str] = []    # last hypothesis for the live window
        self._flushed = False
        self._closed = False          # the dictation ended: commit no more

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def text(self) -> str:
        """The committed stream so far, single-space joined."""
        with self._lock:
            return " ".join(self._words())

    # -- events from the app ---------------------------------------------

    def close(self, dictation_id: int) -> None:
        """The dictation just stopped: commit nothing more (M13).

        A preview decode started while recording finishes SECONDS later, and
        a commit landing after the final patch would type words nobody can
        take back. Segment texts still in flight are unaffected — they are
        authoritative, and the worker consumes them before the patch runs."""
        with self._lock:
            if dictation_id == self._id:
                self._closed = True

    def typed_for(self, dictation_id: int) -> str:
        """What this dictation actually typed into the app, for M13's final
        patch. Empty for any other dictation - a stale final must never
        backspace text the current one is typing."""
        with self._lock:
            return " ".join(self._words()) if dictation_id == self._id else ""

    def on_cut(self, dictation_id: int) -> None:
        """Tier 1 cut: the live window's audio just left the recorder, so
        close the window and start a fresh one. The cut segment's real text
        arrives later, via on_segment_text."""
        with self._lock:
            if dictation_id != self._id:
                return
            self._regions.append(self._window)
            self._window = []
            self._prev = []

    def on_segment_text(self, dictation_id: int, text: str) -> None:
        """The worker finished a cut segment: its text is authoritative for
        that region. Whatever the preview had not committed yet is appended;
        what was already committed stays put (a commit never retracts)."""
        words = text.split()
        with self._lock:
            if dictation_id != self._id:
                return
            if self._reconciled >= len(self._regions):
                self._regions.append([])      # text before its cut: can't
            region = self._regions[self._reconciled]   # happen, but never drop
            # ponytail: aligned by word COUNT, not a diff - the preview and the
            # final pipeline can word the same audio differently. M13's final
            # patch fixes any drift; a diff here would only move the guesswork.
            extra = words[len(region):]
            region.extend(extra)
            self._reconciled += 1
            self._emit(extra, len(self._words()) > len(extra))
        if extra:
            log.info("live commit (segment, +%d): %s", len(extra),
                     " ".join(extra))

    # -- the loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self._tick()
            except Exception:
                log.exception("live preview pass failed - dictation continues")

    def _tick(self) -> None:
        if self.engine is None:
            return
        if not self.is_dictating():
            self._flush()
            return
        dictation_id = self.current_id()
        if dictation_id != self._id:
            self._reset(dictation_id)
        rate = self.recorder.rate
        snap = self.recorder.snapshot()
        if len(snap) < self.min_window_s * rate:
            return
        # Resample OUTSIDE the lock: a real decode must never wait on preview
        # work that doesn't need the engine.
        audio16 = ensure_16k_mono(snap, rate)
        if not self.engine_lock.acquire(blocking=False):
            return          # a segment/final decode owns it - skip this beat
        try:
            t0 = time.perf_counter()
            text, _meta = self.engine.transcribe(audio16, timestamps=False)
        finally:
            self.engine_lock.release()
        log.debug("preview: %.1fs window decoded in %dms -> %r",
                  len(snap) / rate, int((time.perf_counter() - t0) * 1000), text)
        self._commit(text.split(), dictation_id)

    def _commit(self, words: list[str], dictation_id: int) -> None:
        with self._lock:
            if dictation_id != self._id or self._closed:
                return          # the dictation ended while we were decoding
            if self._reconciled < len(self._regions):
                # A cut segment's authoritative text is still in flight, and
                # its words belong BEFORE anything this window commits. Typed
                # text cannot be inserted after the fact, so hold the beat;
                # _prev still advances, so the tick after it lands commits at
                # once. Costs a ~1s typing pause per segment cut (every 30s).
                self._prev = words
                return
            n = commit_prefix(self._prev, words, len(self._window))
            fresh = words[len(self._window):n]
            self._prev = words
            self._window.extend(fresh)
            total = len(self._words())
            self._emit(fresh, total > len(fresh))
        if fresh:
            log.info("live commit (+%d, %d total): %s", len(fresh), total,
                     " ".join(fresh))

    def _emit(self, fresh: list[str], preceded: bool) -> None:
        """Type a newly committed chunk (M13). Called under _lock so the app
        receives chunks in exactly the order text() reports them - the stream
        is append-only, and typing cannot be reordered afterwards."""
        if not fresh or self.on_commit is None:
            return
        try:
            self.on_commit((" " if preceded else "") + " ".join(fresh))
        except Exception:
            log.exception("live typing failed - the final patch still runs")

    def _reset(self, dictation_id: int | None) -> None:
        with self._lock:
            self._id = dictation_id
            self._regions = []
            self._reconciled = 0
            self._window = []
            self._prev = []
            self._flushed = False
            self._closed = False

    def _flush(self) -> None:
        """Dictation over: log the committed stream, once. The state is kept
        until the NEXT dictation resets it, so a segment text still in flight
        can land instead of racing a teardown."""
        with self._lock:
            if self._id is None or self._flushed:
                return
            self._flushed = True
            words = self._words()
        log.info("live stream ended - %d words committed: %s",
                 len(words), " ".join(words))

    def _words(self) -> list[str]:
        out: list[str] = []
        for region in self._regions:
            out.extend(region)
        out.extend(self._window)
        return out
