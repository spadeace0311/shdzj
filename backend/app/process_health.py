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
    process_modes = {
        "worker": ("app.artifacts.worker", "worker"),
        "dispatcher": ("app.artifacts.worker", "dispatcher"),
        "collaboration": ("app.collaboration.worker",),
    }
    try:
        expected = process_modes[mode]
    except KeyError as exc:
        raise ValueError(
            "mode must be worker, dispatcher, or collaboration"
        ) from exc
    current_pid = os.getpid() if own_pid is None else own_pid
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
        if any(
            expected == window
            for window in zip(
                parts,
                *(
                    parts[offset:]
                    for offset in range(1, len(expected))
                ),
            )
        ):
            return pid
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "process",
        choices=("worker", "dispatcher", "collaboration"),
    )
    args = parser.parse_args()
    return 0 if find_process(args.process) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
