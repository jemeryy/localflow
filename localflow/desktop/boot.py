"""Entry-point shim for the localflow / localflow-tray scripts.

Under the windowless gui-script (pythonw) an import-time failure in the app
(a bad edit, a broken venv) kills the process with NOTHING written anywhere.
Import the real app under a catcher that records the reason in crash.log
first, THEN run it.
"""

from __future__ import annotations


def main(argv=None) -> int:
    try:
        from .app import main as app_main
    except BaseException:
        import os
        import time
        import traceback
        from pathlib import Path

        crash_dir = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                     / "LocalFlow")
        try:
            crash_dir.mkdir(parents=True, exist_ok=True)
            with open(crash_dir / "crash.log", "a", encoding="utf-8") as f:
                f.write(f"--- LocalFlow failed to import "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
    return app_main(argv)
