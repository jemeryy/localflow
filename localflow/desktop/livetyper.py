"""M12/M13: the streaming preview engine — append-only committed stream.

While a dictation runs, this thread re-decodes the recorder's uncommitted
buffer about once a second and TYPES newly committed words into the target
app. A word is committed once two consecutive decodes agree on it
(LocalAgreement-2, `core.live.commit_prefix`) — and committed means FINAL:
no WORD typed is ever revised, backspaced or rewritten, mid-dictation or
after (Jeremy 27 Jul: "the streaming is fine, no correction — just no
duplicates or extra words when I stop"). A word Whisper later changes its
mind about simply stays as first typed; the trade for a perfectly calm
stream. The ONE sanctioned edit is punctuation-only (Jeremy 30 Jul): the
trailing mark of the last typed word (any of . , ! ? ; :) stays
provisional until the next words arrive, then is added, replaced or
removed to match what Whisper decided with audio across the pause — see
`_append`.

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

from localflow.core import collapse_repeats, ensure_16k_mono, \
    strip_hallucinations
from localflow.core.live import _norm, append_tail, commit_prefix

log = logging.getLogger("localflow.desktop.livetyper")

POLL_INTERVAL_S = 0.7
MIN_WINDOW_S = 1.5      # a shorter window decodes to noise; wait for audio
_SENTENCE_END = ".!?"   # pause artifacts the fallback heuristic may drop
_PUNCT = ".,!?;:"       # marks the boundary rule may add/replace/remove
_OVERLAP_MAX_WORDS = 8  # longest already-typed phrase the guard may drop


def _first_alpha(word: str) -> str:
    for ch in word:
        if ch.isalpha():
            return ch
    return ""


def _split_punct(word: str) -> tuple[str, str]:
    """"about?" -> ("about", "?"); "don't" -> ("don't", "")."""
    bare = word.rstrip(_PUNCT)
    return bare, word[len(bare):]


class LiveTyper:
    """Polls a StreamRecorder while it records and types committed words.

    `is_dictating() -> bool` and `current_id() -> int` are supplied by the app
    (a mic test also puts the recorder in `recording`, and that audio is not a
    dictation); `engine` is set after warmup, like the Segmenter's VAD.
    `on_patch(n_backspaces, text)` types into the target app; n_backspaces is
    non-zero ONLY for the punctuation-only pause edit (see `_append`) — words
    are never backspaced. Always called under `_lock`, so the screen is
    mutated in exactly the order this class accounts for it.
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
        # how many of its words have AGREED (an index into that hypothesis).
        # `_typed` is how many words the live window actually put on screen -
        # kept separate because a newer hypothesis can shift its indices
        # (merge/split/insert behind the commit), so the two counts drift
        # apart; conflating them was the root cause of the 30 Jul
        # duplications. `_pending_counts` holds the typed word count of each
        # cut window (FIFO), consumed when its authoritative segment text
        # arrives to append the missing tail.
        self._lock = threading.Lock()
        self._id: int | None = None
        self._prev: list[str] = []
        self._committed = 0
        self._typed = 0
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
            self._pending_counts.append(self._typed)
            self._prev = []
            self._committed = 0
            self._typed = 0

    def on_segment_text(self, dictation_id: int, text: str) -> None:
        """Authoritative text for the OLDEST unreconciled window — every mid
        cut segment, and the final drain on release (the worker calls this
        for both, in queue order). Appends only the words the preview had
        not typed for that window; never rewrites anything."""
        with self._lock:
            if dictation_id != self._id or self._discard:
                return
            final = False
            if self._pending_counts:
                count = self._pending_counts.pop(0)
            else:
                final = True                  # the final, uncut window
                count = self._typed
                self._typed = 0
                self._committed = 0
                self._prev = []
            all_words = text.split()
            # By FIFO order the screen's last `count` words are exactly this
            # window's typed words (typing pauses while a cut awaits its
            # text), so the tail is found by aligning them into the
            # authoritative text - content, not position.
            screen_words = self._screen.split()
            window = screen_words[-count:] if count else []
            tail, boundary = append_tail(window, all_words, count)
            self._append(tail, final=final, boundary=boundary)

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
        # The same silence-artifact filters the final pass uses: a pause must
        # never type "You." / "Thank you." (live 27 Jul), and a decode-loop
        # stutter in the hypothesis ("coming up with up with") must collapse
        # BEFORE two decodes can agree on it and commit (live 30 Jul).
        # gapped=True is preview-only: a committed loop is permanent here,
        # while the final/history text keeps genuine near-repeats intact.
        self._revise(collapse_repeats(strip_hallucinations(text),
                                      gapped=True).split(),
                     dictation_id)

    def _revise(self, words: list[str], dictation_id: int) -> None:
        with self._lock:
            if dictation_id != self._id or self._closed:
                return          # the dictation ended while we were decoding
            if not words:
                return          # a flaky empty decode changes nothing
            committed = commit_prefix(self._prev, words, self._committed)
            old = self._committed
            self._prev = words
            self._committed = committed
            # Type the agreed words the screen doesn't have yet - found by
            # aligning the window's typed words into the agreed prefix
            # (content, not position: index drift between hypotheses was
            # the 30 Jul duplication bug). The boundary - the newer surface
            # of the last typed word - resolves its punctuation.
            screen_words = self._screen.split()
            window = screen_words[-self._typed:] if self._typed else []
            fresh, boundary = append_tail(window, words[:committed], old)
            self._typed += self._append(fresh, boundary=boundary)

    def _append(self, words: list[str], final: bool = False,
                boundary: str | None = None) -> int:
        """Type `words` after everything already typed (under _lock).

        Boundary punctuation (live 29/30 Jul): Whisper punctuates a pause
        like a sentence end, and only a LATER decode — hearing across the
        pause — knows whether a mark really belongs there. So the trailing
        punctuation of the last typed word stays PROVISIONAL until the
        next words arrive, and is then edited to match `boundary`: that
        same word's surface in the resolving source (the newer hypothesis,
        or the authoritative segment/final text). The edit may REMOVE a
        mark ("Who said? that" -> "Who said that"), REPLACE it ("mods.
        I" -> "mods, I"), or ADD one that arrived late ("about What" ->
        "about? What"). Punctuation only, ever — Jeremy 30 Jul — and only
        when the boundary word matches the typed word (normalised); on a
        mismatch, or across a Tier 1 cut where no later decode re-hears
        the boundary, the fallback drops a trailing . ! ? before a
        lowercase continuation and otherwise leaves the screen alone."""
        words = list(words)
        n_back = 0
        prefix = ""
        screen_words = self._screen.split()
        # Overlap guard: alignment drift or a decode-loop stutter would
        # re-type words already on screen — seen live 30 Jul as "retire
        # retire" (one word, preview path) and "coming up with up with"
        # (two words), so the guard lives here where both paths (_revise
        # and on_segment_text) converge and matches the longest phrase, not
        # just one word. The last dropped word's newer surface still
        # resolves the boundary mark. Dropping a genuine immediate repeat
        # is the cheaper mistake (Jeremy 27 Jul: no duplicates).
        for k in range(min(len(words), len(screen_words), _OVERLAP_MAX_WORDS),
                       0, -1):
            head = [_norm(w) for w in words[:k]]
            if all(head) and head == [_norm(w) for w in screen_words[-k:]]:
                boundary = words[k - 1]
                del words[:k]
                break
        if (words or final) and screen_words:
            bare, cur = _split_punct(screen_words[-1])
            want = cur
            if (boundary is not None and bare
                    and _norm(boundary) == _norm(screen_words[-1])):
                want = _split_punct(boundary)[1]
            elif words and cur and any(c in _SENTENCE_END for c in cur):
                first = _first_alpha(words[0])
                if first and first.islower():
                    want = ""       # pause artifact: the sentence continued
            if bare and want != cur:
                n_back = len(cur)
                if n_back:
                    self._screen = self._screen[:-n_back]
                prefix = want
        out = prefix
        if words:
            out += (" " if self._screen else "") + " ".join(words)
        if not out and not n_back:
            return 0
        self._screen += out
        log.info("live type (%s+%r)", f"-{n_back} " if n_back else "", out)
        if self.on_patch is not None:
            try:
                self.on_patch(n_back, out)
            except Exception:
                log.exception("live typing failed - dictation continues")
        return len(words)     # words now on screen (the caller's accounting)

    def _reset(self, dictation_id: int | None) -> None:
        with self._lock:
            self._id = dictation_id
            self._prev = []
            self._committed = 0
            self._typed = 0
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
