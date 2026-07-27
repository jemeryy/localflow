"""M12/M13: the streaming preview engine — Google-style interim results.

While a dictation runs, this thread re-decodes the recorder's uncommitted
buffer about once a second and TYPES the current hypothesis into the focused
app straight away, revising what it already typed (backspace + retype) when a
newer decode disagrees. That is the interim-results model Google's streaming
STT uses: words appear the moment they are recognised, marked non-final, and
are corrected in place; the final pass on release is the is_final result —
the worker patches the screen to the authoritative text once, via
`typed_for()` and `core.live.patch_plan`.

Three things keep the stream calm:
- the NEWEST word of a hypothesis is held back until two consecutive decodes
  agree on it (the live edge flickers while a word is still being spoken),
- every hypothesis goes through `strip_hallucinations` first, so a pause
  never types Whisper's silence artifacts ("You.", "Thank you."),
- only the last REVISE_TAIL_WORDS typed words are ever revised mid-dictation.
  Older words are FROZEN until the final patch — a decode that rethinks the
  start of the sentence can no longer rewrite the whole line every beat
  (live 27 Jul: the sentence was retyped over and over).
Revisions use `patch_plan(loose=True)`, which ignores punctuation/case drift
in already-typed words — only a genuinely different word triggers keystrokes.

Threading: same discipline as the Segmenter — this thread only READS the
recorder (snapshot) and never queues work. The engine is strictly
single-threaded, so a preview decode takes the app's engine lock
NON-BLOCKING and skips the beat whenever a segment or the final decode owns
it. A preview must never make a real dictation wait behind it.

Preview decodes are the FAST path: no timestamps, no preprocessing. The
final pass owns quality; this loop owns feel.
"""

from __future__ import annotations

import logging
import threading
import time

from localflow.core import ensure_16k_mono, strip_hallucinations
from localflow.core.live import patch_plan

log = logging.getLogger("localflow.desktop.livetyper")

POLL_INTERVAL_S = 0.7
MIN_WINDOW_S = 1.5      # a shorter window decodes to noise; wait for audio
REVISE_TAIL_WORDS = 4   # only this many trailing words are revisable live


class LiveTyper:
    """Polls a StreamRecorder while it records and live-types the hypothesis.

    `is_dictating() -> bool` and `current_id() -> int` are supplied by the app
    (a mic test also puts the recorder in `recording`, and that audio is not a
    dictation); `engine` is set after warmup, like the Segmenter's VAD.
    `on_patch(n_backspaces, text)` applies one revision to the focused app;
    it is always called under `_lock`, so the screen is mutated in exactly
    the order this class accounts for it.
    """

    def __init__(self, recorder, engine_lock, is_dictating, current_id,
                 engine=None, poll_interval_s: float = POLL_INTERVAL_S,
                 min_window_s: float = MIN_WINDOW_S, on_patch=None):
        self.recorder = recorder
        self.engine = engine
        self.engine_lock = engine_lock
        self.is_dictating = is_dictating
        self.current_id = current_id
        self.poll_interval_s = poll_interval_s
        self.min_window_s = min_window_s
        self.on_patch = on_patch
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="localflow-livetyper")
        # `_regions` are windows the Tier 1 segmenter already cut away
        # (oldest first, provisional preview text until the segment's
        # authoritative text REPLACES it); `_window` is the live hypothesis.
        # `_screen` is exactly what this dictation has typed into the app —
        # the thing every patch is computed against and what typed_for()
        # hands the final pass.
        self._lock = threading.Lock()
        self._id: int | None = None
        self._regions: list[list[str]] = []
        self._reconciled = 0          # regions already given their segment text
        self._window: list[str] = []
        self._prev: list[str] = []    # last raw hypothesis (edge agreement)
        self._screen = ""
        self._flushed = False
        self._closed = False          # the dictation ended: type no more

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def text(self) -> str:
        """What this dictation has typed into the app so far."""
        with self._lock:
            return self._screen

    # -- events from the app ---------------------------------------------

    def close(self, dictation_id: int) -> None:
        """The dictation just stopped: type nothing more (M13).

        A preview decode started while recording finishes SECONDS later, and
        a revision landing after the final patch would corrupt text nobody
        can take back. The final patch fixes everything in one pass, so any
        segment text still in flight is skipped too — `_screen` stays exactly
        what is on screen, which is all the patch needs."""
        with self._lock:
            if dictation_id == self._id:
                self._closed = True

    def typed_for(self, dictation_id: int) -> str:
        """What this dictation actually typed into the app, for M13's final
        patch. Empty for any other dictation - a stale final must never
        backspace text the current one is typing."""
        with self._lock:
            return self._screen if dictation_id == self._id else ""

    def on_cut(self, dictation_id: int) -> None:
        """Tier 1 cut: the live window's audio just left the recorder, so
        freeze the current hypothesis as that region's provisional text and
        start a fresh window. The segment's real text arrives later, via
        on_segment_text, and revises it in place."""
        with self._lock:
            if dictation_id != self._id:
                return
            self._regions.append(self._prev)
            self._window = []
            self._prev = []
            self._apply()       # the held edge word types now: audio is final

    def on_segment_text(self, dictation_id: int, text: str) -> None:
        """The worker finished a cut segment: its text REPLACES the preview's
        provisional text for that region, and the screen is revised to match.
        Called for every mid segment, silent ones included — reconciliation
        is in cut order, so skipping one would shift every later segment onto
        the wrong region."""
        with self._lock:
            if dictation_id != self._id or self._closed:
                return          # the final patch will fix everything at once
            if self._reconciled >= len(self._regions):
                self._regions.append([])      # text before its cut: can't
            self._regions[self._reconciled] = text.split()   # happen, but
            self._reconciled += 1                            # never drop it
            self._apply()

    # -- the loop ---------------------------------------------------------

    def _loop(self) -> None:
        wait = self.poll_interval_s
        while not self._stop.wait(wait):
            t0 = time.perf_counter()
            failed = False
            try:
                self._tick()
            except Exception:
                failed = True
                log.exception("live preview pass failed - dictation continues")
            spent = time.perf_counter() - t0
            # Tick on the poll interval while decodes are cheap, but never
            # spend more than half the wall clock decoding. Back-to-back
            # previews on a long window starve the final decode of the engine
            # lock and were what pushed the shared-memory iGPU into "bad
            # allocation" (live 27 Jul).
            wait = max(self.poll_interval_s - spent, spent)
            if failed:
                # A failed decode is nearly always memory pressure on the
                # shared-memory iGPU. Retrying every beat under pressure is
                # what turns a hiccup into a spiral - give it room instead.
                wait = max(wait, self.poll_interval_s * 3)

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
            # rebuild_on_failure=False: a preview is disposable. Letting it
            # trigger the CPU fallback would stall ~15s under the engine lock
            # and demote every later dictation to CPU over one skippable beat.
            text, _meta = self.engine.transcribe(audio16, timestamps=False,
                                                 rebuild_on_failure=False)
        finally:
            self.engine_lock.release()
        log.debug("preview: %.1fs window decoded in %dms -> %r",
                  len(snap) / rate, int((time.perf_counter() - t0) * 1000), text)
        # The same silence-artifact filter the final pass uses: a pause must
        # never type "You." / "Thank you." (live 27 Jul).
        self._revise(strip_hallucinations(text).split(), dictation_id)

    def _revise(self, words: list[str], dictation_id: int) -> None:
        with self._lock:
            if dictation_id != self._id or self._closed:
                return          # the dictation ended while we were decoding
            if not words and self._window:
                return  # one empty decode never erases typed words; the
                        # final pass erases them if it was genuinely silence
            # Hold the newest word until two consecutive decodes agree on the
            # whole hypothesis — the live edge flickers while a word is still
            # being spoken. Everything older types immediately (interim
            # results); a later decode revises it if Whisper changes its mind.
            stable = words == self._prev
            self._prev = words
            self._window = words if stable else words[:-1]
            self._apply()

    def _apply(self) -> None:
        """Revise the screen to match the current desired text (under _lock)."""
        desired_words: list[str] = []
        for region in self._regions:
            desired_words.extend(region)
        desired_words.extend(self._window)
        # Freeze everything but the last few typed words: a decode (or a
        # segment reconciliation) that changes its mind about older words is
        # ignored — the final patch fixes real errors ONCE at the end.
        # Bounded rewrites are what make the stream watchable (live 27 Jul).
        screen_words = self._screen.split()
        frozen = len(screen_words) - REVISE_TAIL_WORDS
        if frozen > 0:
            desired_words[:frozen] = screen_words[:frozen]
        desired = " ".join(desired_words)
        n_back, retype = patch_plan(self._screen, desired, loose=True)
        if not n_back and not retype:
            return
        self._screen = self._screen[:len(self._screen) - n_back] + retype
        log.info("live type (-%d chars, +%r)", n_back, retype)
        if self.on_patch is None:
            return
        try:
            self.on_patch(n_back, retype)
        except Exception:
            log.exception("live typing failed - the final patch still runs")

    def _reset(self, dictation_id: int | None) -> None:
        with self._lock:
            self._id = dictation_id
            self._regions = []
            self._reconciled = 0
            self._window = []
            self._prev = []
            self._screen = ""
            self._flushed = False
            self._closed = False

    def _flush(self) -> None:
        """Dictation over: log what was typed, once. The state is kept until
        the NEXT dictation resets it, so typed_for() stays answerable while
        the worker runs the final patch."""
        with self._lock:
            if self._id is None or self._flushed:
                return
            self._flushed = True
            screen = self._screen
        log.info("live stream ended - on screen: %s", screen)
