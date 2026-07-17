"""Hardened one-shot Codex CLI wrapper for the plan/review skills.

Why this exists (reflection 2026-07-02): raw Codex-broker calls stalled, hung,
or died mid-review in 12+ sessions. The recurring failure modes, each handled
here deterministically:

- Windows taskkill leaks "SUCCESS: ... terminated" onto STDOUT, corrupting any
  parse of codex's stdout (memory reference_codex_down_windows_stdout_taskkill).
  -> We never parse stdout: `codex exec -o <file>` writes the final message to
     a file and we read THAT.
- Huge review prompts blew Windows' argv limit ("arg list too long").
  -> The prompt is always piped via STDIN, never argv.
- Calls hung for 2-10 minutes with no cap.
  -> Hard wall-clock timeout; on expiry the whole process TREE is killed
     (taskkill /T on Windows) and we exit with a distinct code.
- Usage-limit / auth failures were retried pointlessly.
  -> Detected and surfaced immediately with the reset info; never retried.
- Genuine transient failures (broker JSONL noise, empty output) needed a
  manual "retry codex".
  -> One automatic retry, then a clear failure.

Usage (from a skill or Bash):
    python tools/codex_exec.py --prompt-file .tmp/prompt.txt [--resume-last]
        [--effort high] [--write] [--timeout-sec 600] [--cd <dir>]
    python tools/codex_exec.py --pong          # availability preflight only

Output contract: the agent's final message is printed to STDOUT (nothing
else); diagnostics go to STDERR. Exit codes: 0 ok, 3 usage-limit, 4 timeout,
5 auth, 6 codex CLI not found, 1 other failure (after the retry).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PONG_PROMPT = "Reply with exactly: PONG"
PONG_TIMEOUT_SEC = 120
DEFAULT_TIMEOUT_SEC = 600

USAGE_LIMIT_MARKERS = ("usage limit", "you've hit", "rate limit", "429")
AUTH_MARKERS = ("401", "unauthorized", "not logged in", "authentication")


def log(msg: str) -> None:
    print(f"[codex_exec] {msg}", file=sys.stderr, flush=True)


def resolve_codex() -> list[str]:
    """Absolute launcher argv for the codex CLI. npm shims (.cmd/.bat) cannot be
    exec'd directly by CreateProcess, so they go through cmd /c."""
    path = shutil.which("codex")
    if not path:
        return []
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path]
    return [path]


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        else:
            # Popen runs with start_new_session=True on POSIX, so killing the
            # process group takes the whole tree, not just the shim.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception as e:
        log(f"WARN: kill_tree failed: {e}")


def classify(text: str) -> str | None:
    low = text.lower()
    if any(m in low for m in USAGE_LIMIT_MARKERS):
        return "usage-limit"
    if any(m in low for m in AUTH_MARKERS):
        return "auth"
    return None


def run_once(codex: list[str], prompt: str, *, resume_last: bool, effort: str | None,
             write: bool, timeout_sec: int, cd: str | None) -> tuple[str, str, str]:
    """One codex exec run. Returns (status, final_message, raw_diagnostics) where
    status is 'ok' | 'usage-limit' | 'auth' | 'timeout' | 'fail'."""
    fd, out_path = tempfile.mkstemp(prefix="codex_last_", suffix=".txt")
    os.close(fd)  # keep only the path; an open handle blocks unlink on Windows
    out_file = Path(out_path)
    cmd = list(codex) + ["exec"]
    if resume_last:
        # `exec resume` takes -o/-c but NOT -s/--color/--cd; sandbox and working
        # root are inherited from the resumed session.
        cmd += ["resume", "--last", "-o", str(out_file)]
        if cd:
            log("NOTE: --cd ignored on --resume-last (inherited from the session)")
    else:
        cmd += ["-s", "workspace-write" if write else "read-only",
                "-o", str(out_file), "--color", "never"]
        if cd:
            cmd += ["--cd", cd]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd += ["-"]  # prompt from stdin: immune to argv length limits

    log(f"exec: {' '.join(cmd[:6])} ... (timeout {timeout_sec}s)")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            start_new_session=(os.name != "nt"),
        )
    except OSError as e:
        return "fail", "", f"launch failed: {e}"
    try:
        stdout, _ = proc.communicate(input=prompt, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out_file.unlink(missing_ok=True)
        except OSError:
            pass
        return "timeout", "", f"no completion within {timeout_sec}s; process tree killed"
    stdout = stdout or ""

    final = ""
    try:
        if out_file.exists():
            final = out_file.read_text(encoding="utf-8", errors="replace").strip()
            out_file.unlink(missing_ok=True)
    except OSError as e:
        log(f"WARN: could not read output file: {e}")

    # Classify FAILURES from console output only, and only when there is no
    # final message: a legitimate review whose findings mention "401" or
    # "rate limit" must never be misread as an infra failure.
    verdict = classify(stdout) if not final else None
    if verdict == "usage-limit":
        hint = next((ln.strip() for ln in stdout.splitlines()
                     if "limit" in ln.lower()), "usage limit hit")
        return "usage-limit", final, hint
    if verdict == "auth" and not final:
        return "auth", "", "auth failure; run /codex:setup to re-authenticate"
    if proc.returncode != 0 and not final:
        tail = "\n".join(stdout.splitlines()[-8:])
        return "fail", "", f"exit {proc.returncode}; tail:\n{tail}"
    if not final:
        return "fail", "", "codex returned an empty final message"
    return "ok", final, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt", default=None, help="inline prompt text")
    ap.add_argument("--prompt-file", default=None, help="file containing the prompt (preferred)")
    ap.add_argument("--resume-last", action="store_true",
                    help="resume the most recent codex session instead of starting fresh")
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high"],
                    help="model reasoning effort")
    ap.add_argument("--write", action="store_true",
                    help="workspace-write sandbox (default read-only: plan/review calls)")
    ap.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    ap.add_argument("--cd", default=None, help="working root for the agent")
    ap.add_argument("--pong", action="store_true", help="availability preflight only")
    args = ap.parse_args()

    codex = resolve_codex()
    if not codex:
        log("codex CLI not found on PATH")
        return 6

    if args.pong:
        status, final, diag = run_once(
            codex, PONG_PROMPT, resume_last=False, effort="low", write=False,
            timeout_sec=PONG_TIMEOUT_SEC, cd=args.cd)
        if status == "ok" and "PONG" in final.upper():
            print("PONG")
            return 0
        log(f"preflight {status}: {diag or final}")
        return {"usage-limit": 3, "timeout": 4, "auth": 5}.get(status, 1)

    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")
    elif args.prompt:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        log("no prompt given (--prompt, --prompt-file, or stdin)")
        return 1
    if not prompt.strip():
        log("prompt is empty")
        return 1

    for attempt in (1, 2):
        status, final, diag = run_once(
            codex, prompt, resume_last=args.resume_last, effort=args.effort,
            write=args.write, timeout_sec=args.timeout_sec, cd=args.cd)
        if status == "ok":
            print(final)
            return 0
        if status == "usage-limit":
            log(f"USAGE LIMIT: {diag} (not retrying; check chatgpt.com/codex usage)")
            return 3
        if status == "auth":
            log(f"AUTH: {diag} (not retrying)")
            return 5
        if status == "timeout":
            log(f"TIMEOUT: {diag}")
            return 4
        log(f"attempt {attempt} failed: {diag}")
        if attempt == 1:
            time.sleep(5)
    return 1


if __name__ == "__main__":
    sys.exit(main())
