from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

RESTART_DELAY_SECONDS = float(os.getenv("EMBEDDER_RESTART_DELAY_SECONDS", "2"))
MAX_CRASH_RESTARTS = int(os.getenv("EMBEDDER_MAX_CRASH_RESTARTS", "0"))  # 0 = unlimited


@dataclass
class ManagedProcess:
    name: str
    cmd: list[str]
    env: dict[str, str] | None = None
    proc: subprocess.Popen | None = None


stop_requested = False


def _build_processes() -> list[ManagedProcess]:
    base_env = os.environ.copy()

    embedder_env = base_env.copy()
    embedder_env.setdefault("OCR_SCANNER_BASE_URL", "http://127.0.0.1:3300")
    embedder_env.setdefault("AUDIO_TRANSCRIPTION_BASE_URL", "http://127.0.0.1:3400")

    ocr_env = base_env.copy()
    ocr_env.setdefault("OCR_API_PORT", "3300")

    audio_env = base_env.copy()
    audio_env.setdefault("AUDIO_API_PORT", "3400")

    return [
        ManagedProcess(name="embedder-worker", cmd=["python", "-m", "apps.embedder.worker"], env=embedder_env),
        ManagedProcess(name="ocr-scanner", cmd=["python", "apps/ocr-scanner/worker.py"], env=ocr_env),
        ManagedProcess(name="audio-transcription", cmd=["python", "apps/audio-transcription/worker.py"], env=audio_env),
    ]


def _terminate_all(processes: list[ManagedProcess], signum: int = signal.SIGTERM) -> None:
    for managed in processes:
        proc = managed.proc
        if not proc or proc.poll() is not None:
            continue
        try:
            proc.send_signal(signum)
        except Exception:
            proc.terminate()


def _wait_for_all(processes: list[ManagedProcess], timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    for managed in processes:
        proc = managed.proc
        if not proc:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def _handle_signal(signum, _frame):
    global stop_requested
    stop_requested = True


def _run_once() -> int:
    processes = _build_processes()
    for managed in processes:
        print(f"[embedder-python] starting {managed.name}: {' '.join(managed.cmd)}", flush=True)
        managed.proc = subprocess.Popen(managed.cmd, env=managed.env)

    try:
        while not stop_requested:
            for managed in processes:
                proc = managed.proc
                if not proc:
                    continue
                exit_code = proc.poll()
                if exit_code is not None:
                    print(
                        f"[embedder-python] process {managed.name} exited with code {exit_code}; stopping stack.",
                        flush=True,
                    )
                    _terminate_all(processes)
                    _wait_for_all(processes)
                    return exit_code
            time.sleep(0.5)
    finally:
        _terminate_all(processes)
        _wait_for_all(processes)

    return 0


def run_forever() -> int:
    crash_count = 0
    while not stop_requested:
        exit_code = _run_once()
        if stop_requested:
            return 0
        if exit_code == 0:
            return 0

        crash_count += 1
        if MAX_CRASH_RESTARTS > 0 and crash_count >= MAX_CRASH_RESTARTS:
            print("[embedder-python] max crash restarts reached; exiting.", flush=True)
            return exit_code

        print(
            f"[embedder-python] stack crashed with exit code {exit_code}; restarting in {RESTART_DELAY_SECONDS}s.",
            flush=True,
        )
        time.sleep(RESTART_DELAY_SECONDS)

    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run_forever()


if __name__ == "__main__":
    sys.exit(main())
