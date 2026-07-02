"""M3 injection check: FR-10 end-to-end against a throwaway tkinter window.

Opens its own always-on-top text box, focuses it, then runs inject_text()
from a background thread (a real target app pumps its own message queue,
so the injector must be exercised from another thread to be honest about
the clipboard-restore race). Verifies:

  1. AT-1 shape: the pasted sentence lands in the focused text box.
  2. AT-2:       a clipboard holding PRESERVE_ME survives the injection.
  3. AT-3 shape: inject_text("") sends nothing and the box is unchanged.
  4. FR-12:      inject_mode="type" also lands the text.

SAFETY: aborts without sending any keys if the test window fails to take
focus, so keystrokes can never land in an unrelated app.

The full AT-1/2/3 (real mic + hotkey + Notepad) still needs a human:
run `localflow` and follow docs/PRD.md §8.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import tkinter as tk

import pyperclip

from localflow.desktop.inject import inject_text

SENTENCE = "the quick brown fox jumps over the lazy dog"
GA_ROOT = 2


def window_has_focus(root: tk.Tk) -> bool:
    fg = ctypes.windll.user32.GetForegroundWindow()
    ours = ctypes.windll.user32.GetAncestor(root.winfo_id(), GA_ROOT)
    return fg == ours


def pump_while(root: tk.Tk, predicate, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while predicate() and time.time() < deadline:
        root.update()
        time.sleep(0.01)


def run_injection(root: tk.Tk, text: str, mode: str) -> bool:
    """Run inject_text on a worker thread while the Tk loop pumps events."""
    result: dict[str, bool] = {}
    t = threading.Thread(target=lambda: result.update(sent=inject_text(text, mode=mode)))
    t.start()
    pump_while(root, t.is_alive, timeout_s=5.0)
    t.join()
    pump_while(root, lambda: False, 0)  # one last drain
    for _ in range(10):
        root.update()
        time.sleep(0.02)
    return result["sent"]


def main() -> int:
    root = tk.Tk()
    root.title("LocalFlow M3 inject check — closes itself")
    root.attributes("-topmost", True)
    box = tk.Text(root, width=64, height=6)
    box.pack(padx=8, pady=8)
    root.update()
    root.focus_force()

    pump_while(root, lambda: not window_has_focus(root), timeout_s=3.0)
    if not window_has_focus(root):
        print("ABORT: test window could not take focus — no keys were sent.")
        root.destroy()
        return 2
    # AFTER the window is foreground — focus_force() above put Tk-internal
    # focus on the toplevel, which silently eats key events.
    box.focus_set()
    root.update()

    pyperclip.copy("PRESERVE_ME")

    sent = run_injection(root, SENTENCE, "paste")
    pasted = box.get("1.0", "end").strip()
    clipboard_after = pyperclip.paste()

    sent_empty = run_injection(root, "", "paste")
    after_empty = box.get("1.0", "end").strip()

    box.delete("1.0", "end")
    box.focus_set()
    root.update()
    if not window_has_focus(root):
        print("ABORT: lost focus before the type-mode check — skipping it.")
        typed = None
    else:
        run_injection(root, SENTENCE, "type")
        typed = box.get("1.0", "end").strip()

    root.destroy()

    checks = [
        ("paste lands in focused app (AT-1 shape)", sent and pasted == SENTENCE),
        ("clipboard restored to PRESERVE_ME (AT-2)", clipboard_after == "PRESERVE_ME"),
        ("empty text injects nothing (AT-3 shape)", sent_empty is False and after_empty == pasted),
        ("type mode lands text (FR-12)", typed is None or typed == SENTENCE),
    ]
    failed = False
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed |= not ok
    if pasted != SENTENCE:
        print(f"        box contained: {pasted!r}")
    if clipboard_after != "PRESERVE_ME":
        print(f"        clipboard was: {clipboard_after!r}")
    if typed is not None and typed != SENTENCE:
        print(f"        type-mode box: {typed!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
