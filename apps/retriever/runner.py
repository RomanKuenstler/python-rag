from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

RETRIEVER_CMD = ["node", "apps/retriever/api.js"]
RESTART_DELAY_SECONDS = float(os.getenv("RETRIEVER_RESTART_DELAY_SECONDS", "2"))
MAX_CRASH_RESTARTS = int(os.getenv("RETRIEVER_MAX_CRASH_RESTARTS", "0"))  # 0 = unlimited

child: subprocess.Popen | None = None
stop_requested = False


def _handle_signal(signum, _frame):
    global stop_requested
    stop_requested = True
    if child and child.poll() is None:
        try:
            child.send_signal(signum)
        except Exception:
            child.terminate()


def run_forever() -> int:
    global child
    crash_count = 0

    while not stop_requested:
        print(f"[retriever-python] starting subprocess: {' '.join(RETRIEVER_CMD)}", flush=True)
        child = subprocess.Popen(RETRIEVER_CMD)
        exit_code = child.wait()
        child = None

        if stop_requested:
            return exit_code

        if exit_code == 0:
            print("[retriever-python] retriever exited cleanly; stopping supervisor.", flush=True)
            return 0

        crash_count += 1
        print(f"[retriever-python] retriever crashed with exit code {exit_code}.", flush=True)
        if MAX_CRASH_RESTARTS > 0 and crash_count >= MAX_CRASH_RESTARTS:
            print("[retriever-python] max crash restarts reached; exiting.", flush=True)
            return exit_code

        time.sleep(RESTART_DELAY_SECONDS)

    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run_forever()


if __name__ == "__main__":
    sys.exit(main())
