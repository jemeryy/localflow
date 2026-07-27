"""M12/M13: the streaming preview engine — append-only committed stream.

While a dictation runs, this thread re-decodes the recorder's uncommitted
buffer about once a second and TYPES newly committed words into the target
app. A word is committed once two consecutive decodes agree on it
(LocalAgreement-2, `core.live.commit_prefix`) — and committed means FINAL:
nothing typed is ever revised, backspaced or rewritten, mid-dictation or
after (Jeremy 27 Jul: "the streaming is fine, no correction — just no
duplicates or extra words when I stop"). A word Whisper later changes its
mind about simply stays as first typed; the trade for a perfectly calm
stream.

The only typing after the hotkey is released is the TAIL: the authoritative
decode of each drained window appends the words the preview had not typed
yet (`on_segment_text`, called by the worker for mid cuts and for the final
drain). Append-only ordering rule: after a Tier 1 cut the preview pauses
until that window's authoritative text has landed, so a cut window's tail
can never appear after the next window's words.

Every hypothesis goes through `strip_hallucinations` first, so a pause never
types Whisper's silence artifacts ("You.", "Thank you.").

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
from localflow.core.live import _norm, commit_prefix

log = logging.getLogger("localflow.desktop.livetyper")

POLL_INTERVAL_S = 0.7
MIN_WINDOW_S = 1.5      # a shorter window decodes to noise; wait for audio


class LiveTyper:
    """Polls a StreamRecorder while it records and types committed words.

    `is_dictating() -> bool` and `current_id() -> int` are supplied by the app
    (a mic test also puts the recorder in `recording`, and that audio is not a
    dictation); `engine` is set after warmup, like the Segmenter's VAD.
    `on_patch(n_backspaces, text)` types into the target app; append-only, so
    n_backspaces is always 0 here (the signature is shared with the final
    silence take-back). Always called under `_lock`, so the screen is mutated
    in exactly the order this class accounts for it.
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
        # `_screen` is exactly what this dictation has typed into the app.
        # `_prev`/`_committed` track the LIVE window: last raw hypothesis and
        # how many of its words are typed. `_pending_counts` holds the typed
        # word count of each cut window (FIFO), consumed when its
        # authoritative segment text arrives to append the missing tail.
        self._lock = threading.Lock()
        self._id: int | None = None
        self._prev: list[str] = []
        self._committed = 0
        self._pending_counts: list[int] = []
        self._screen = ""
        self._flushed = False
        self._closed = False          # dictation ended: no more previews
        self._discard = False         # Esc: segment tails must not type either

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def text(self) -> str:
        """What this dictation has typed into the app so far."""
        with self._lock:
            return self._screen

    # -- events from the app ---------------------------------------------

    def close(self, dictation_id: int, discard: bool = False) -> None:
        """The dictation just stopped: no more PREVIEW typing (a decode
        started while recording lands seconds later). The authoritative
        tails still arrive via on_segment_text — unless `discard` (Esc),
        after which nothing may ever type again."""
        with self._lock:
            if dictation_id == self._id:
                self._closed = True
                if discard:
                    self._discard = True

    def typed_for(self, dictation_id: int) -> str:
        """What this dictation actually typed into the app. Empty for any
        other dictation - a stale final must never touch text the current
        one is streaming."""
        with self._lock:
            return self._screen if dictation_id == self._id else ""

    def on_cut(self, dictation_id: int) -> None:
        """Tier 1 cut: the live window's audio just left the recorder. Record
        how much of it was typed and pause the preview — the window's
        authoritative text (on_segment_text) appends its missing tail, and
        only then may the next window's words follow. Append-only means
        order is everything."""
        with self._lock:
            if dictation_id != self._id:
                return
            self._pending_counts.append(self._committed)
            self._prev = []
            self._committed = 0

    def on_segment_text(self, dictation_id: int, text: str) -> None:
        """Authoritative text for the OLDEST unreconciled window — every mid
        cut segment, and the final drain on release (the worker calls this
        for both, in queue order). Appends only the words the preview had
        not typed for that window; never rewrites anything."""
        with self._lock:
            if dictation_id != self._id or self._discard:
                return
            if self._pending_counts:
                count = self._pending_counts.pop(0)
            else:
                count = self._committed       # the final, uncut window
                self._committed = 0
                self._prev = []
            tail = text.split()[count:]
            # One-word overlap guard: a word-count drift between the preview
            # and the authoritative decode ("I'm" typed vs "I am" decoded)
            # would re-type the last word. Dropping a genuine immediate
            # repeat is the cheaper mistake (Jeremy 27 Jul: no duplicates).
            screen_words = self._screen.split()
            if tail and screen_words and _norm(tail[0]) == _norm(screen_words[-1]):
                tail = tail[1:]
            self._append(tail)

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
        with self._lock:
            if self._pending_counts:
                return    # a cut window awaits its authoritative text; typing
                          # is paused so its tail lands in order — skip the
                          # decode too, it would be thrown away
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
            if not words:
                return          # a flaky empty decode changes nothing
            committed = commit_prefix(self._prev, words, self._committed)
            fresh = words[self._committed:committed]
            self._prev = words
            self._committed = committed
            self._append(fresh)

    def _append(self, words: list[str]) -> None:
        """Type `words` after everything already typed (under _lock)."""
        if not words:
            return
        text = (" " if self._screen else "") + " ".join(words)
        self._screen += text
        log.info("live type (+%r)", text)
        if self.on_patch is None:
            return
        try:
            self.on_patch(0, text)
        except Exception:
            log.exception("live typing failed - dictation continues")

    def _reset(self, dictation_id: int | None) -> None:
        with self._lock:
            self._id = dictation_id
            self._prev = []
            self._committed = 0
            self._pending_counts = []
            self._screen = ""
            self._flushed = False
            self._closed = False
            self._discard = False

    def _flush(self) -> None:
        """Dictation over: log what was typed, once. The state is kept until
        the NEXT dictation resets it, so typed_for() stays answerable while
        the worker appends the final tail."""
        with self._lock:
            if self._id is None or self._flushed:
                return
            self._flushed = True
            screen = self._screen
        log.info("live stream ended - on screen: %s", screen)
