from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def find_process(
    mode: str,
    *,
    proc_root: Path = Path("/proc"),
    own_pid: int | None = None,
) -> int | None:
    if mode not in {"worker", "dispatcher"}:
        raise ValueError("mode must be worker or dispatcher")
    current_pid = os.getpid() if own_pid is None else own_pid
    expected = ("app.artifacts.worker", mode)
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == current_pid:
            continue
        cmdline_path = process_dir / "cmdline"
        try:
            parts = tuple(
                part.decode(errors="replace")
                for part in cmdline_path.read_bytes().split(b"\0")
                if part
            )
        except OSError:
            continue
        if expected in tuple(zip(parts, parts[1:])):
            return pid
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("process", choices=("worker", "dispatcher"))
    args = parser.parse_args()
    return 0 if find_process(args.process) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
