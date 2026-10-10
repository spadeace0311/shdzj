from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.operations.health import read_health_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default=os.environ.get(
            "OPERATIONS_HEALTH_FILE",
            "/tmp/operations-worker-health.json",
        ),
    )
    args = parser.parse_args()
    status, _heartbeat = read_health_payload(Path(args.file))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
