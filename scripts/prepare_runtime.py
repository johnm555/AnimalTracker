#!/usr/bin/env python3
"""Bootstrap the desktop runtime; runs with the user's selected Python."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def emit(event, **kwargs):
    print(json.dumps(dict(event=event, **kwargs)), flush=True)


def install(command):
    child = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    def stop(_signum, _frame):
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
        raise SystemExit(1)

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        if child.wait() != 0:
            raise subprocess.CalledProcessError(child.returncode, command)
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    if sys.version_info < (3, 11):
        emit("error", message="Python 3.11 or newer is required.")
        return 1
    root = Path(__file__).resolve().parents[1]
    data = Path(
        os.environ.get(
            "ANIMAL_TRACKER_DATA",
            Path.home() / "Library/Application Support/AnimalTracker",
        )
    )
    runtime = data / "desktop-runtime"
    try:
        emit("progress", message="Creating a private Python environment…")
        install([sys.executable, "-m", "venv", str(runtime)])
        python = runtime / "bin/python"
        emit(
            "progress",
            message="Installing the tracking engine and local vision libraries. This can take several minutes…",
        )
        install(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(root / "backend/requirements.txt"),
            ]
        )
        emit("result", data={"message": "Tracking engine ready."})
        return 0
    except (OSError, subprocess.CalledProcessError):
        emit(
            "error",
            message="Engine installation failed. Check your internet connection and Python installation, then retry. Python 3.12 is recommended.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
