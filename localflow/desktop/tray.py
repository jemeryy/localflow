"""System tray (FR-14) + recording indicator (FR-2): pystray + Pillow.

Threading: pystray on Windows wants the MAIN thread for its Win32 message
loop — TrayIcon.run() blocks there. set_state() is safe from any thread
(pystray marshals icon/title updates onto the loop); it is called from the
hotkey listener and worker threads. Transcribe/inject stay on the worker,
same rule as pynput.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from localflow.desktop.config import open_settings_editor

log = logging.getLogger("localflow.desktop.tray")

# FR-2: a small unobtrusive dot. Grey = idle, red = recording,
# amber = transcribing/pasting.
_STATE_STYLE = {
    "idle": ((110, 110, 110, 255), "LocalFlow - idle"),
    "rec": ((220, 50, 47, 255), "LocalFlow - recording"),
    "busy": ((245, 166, 35, 255), "LocalFlow - transcribing"),
}


def _dot(rgba: tuple[int, int, int, int], size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = size // 8
    draw.ellipse([pad, pad, size - pad, size - pad], fill=rgba)
    return img


def make_state_images() -> dict[str, Image.Image]:
    return {state: _dot(rgba) for state, (rgba, _) in _STATE_STYLE.items()}


class TrayIcon:
    """Menu: status line, cleanup toggle, pill visibility (M7), reset pill
    position (M7), open settings, quit. Quit stops the icon loop; the
    caller tears the rest down after run() returns."""

    # Jeremy (6 Jul): no "hidden" option anywhere — the pill is either
    # always there or appears while dictating. (Config "never" still
    # exists for --no-overlay compat but no menu offers it.)
    _VISIBILITY_LABELS = (("always", "Always"),
                          ("dictating", "While dictating"))

    def __init__(self, status: str, config_path: Path,
                 cleanup_get=None, cleanup_toggle=None,
                 visibility_get=None, visibility_set=None,
                 reset_pill_position=None):
        self._config_path = config_path
        self._images = make_state_images()
        if cleanup_get is not None:
            # FR-14 toggle: checkmark reflects app.cleanup_active live.
            cleanup_item = pystray.MenuItem(
                "Cleanup (slower, cleaner)",
                lambda icon, item: cleanup_toggle(),
                checked=lambda item: bool(cleanup_get()),
            )
        else:
            cleanup_item = pystray.MenuItem(
                "Cleanup unavailable (no model on disk)", None, enabled=False)
        extra: list[pystray.MenuItem] = []
        if visibility_get is not None and visibility_set is not None:
            # M7: radio submenu; the checked mark follows the live mode and
            # the choice is persisted to localflow.toml by the callback.
            # pystray's _assert_action rejects callables with extra params
            # (even defaulted ones - found live 6 Jul), so bind the mode
            # with plain closures, never `m=mode` lambda defaults.
            def _vis_item(mode: str, label: str) -> pystray.MenuItem:
                def select(icon, item):
                    visibility_set(mode)

                def is_checked(item):
                    return visibility_get() == mode

                return pystray.MenuItem(label, select, checked=is_checked,
                                        radio=True)
            extra.append(pystray.MenuItem("Show pill", pystray.Menu(
                *(_vis_item(m, lbl) for m, lbl in self._VISIBILITY_LABELS))))
        if reset_pill_position is not None:
            extra.append(pystray.MenuItem(
                "Reset pill position",
                lambda icon, item: reset_pill_position()))
        menu = pystray.Menu(
            pystray.MenuItem(status, None, enabled=False),
            cleanup_item,
            *extra,
            pystray.MenuItem("Open settings file", self._open_settings),
            pystray.MenuItem("Quit LocalFlow", self._quit),
        )
        self._icon = pystray.Icon(
            "LocalFlow", self._images["idle"], _STATE_STYLE["idle"][1], menu
        )

    def set_state(self, state: str) -> None:
        """'idle' | 'rec' | 'busy' — safe from any thread; never raises
        (a tray hiccup must not kill a dictation)."""
        try:
            rgba_title = _STATE_STYLE.get(state)
            if rgba_title is None:
                return
            self._icon.icon = self._images[state]
            self._icon.title = rgba_title[1]
        except Exception:
            log.exception("tray state update failed - continuing")

    def _open_settings(self, icon, item) -> None:
        try:
            open_settings_editor(self._config_path)
        except Exception:
            log.exception("could not open settings file %s", self._config_path)

    def _quit(self, icon, item) -> None:
        icon.stop()

    def run(self) -> None:
        """Blocks until Quit. Must be the main thread on Windows."""
        self._icon.run()

    def stop(self) -> None:
        self._icon.stop()
