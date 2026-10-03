from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import process_health


def _write_cmdline(path: Path, *parts: str) -> None:
    path.write_bytes(b"\0".join(part.encode() for part in parts) + b"\0")


@pytest.mark.parametrize("mode", ["worker", "dispatcher"])
def test_find_process_matches_exact_worker_argv(
    tmp_path: Path,
    mode: str,
) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "123"
    process_dir.mkdir(parents=True)
    _write_cmdline(
        process_dir / "cmdline",
        "python",
        "-m",
        "app.artifacts.worker",
        mode,
    )

    assert process_health.find_process(
        mode,
        proc_root=proc_root,
        own_pid=999,
    ) == 123


def test_find_process_matches_collaboration_worker(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "125"
    process_dir.mkdir(parents=True)
    _write_cmdline(
        process_dir / "cmdline",
        "python",
        "-m",
        "app.collaboration.worker",
    )

    assert process_health.find_process(
        "collaboration",
        proc_root=proc_root,
        own_pid=999,
    ) == 125


def test_find_process_ignores_healthcheck_self_and_shell_pattern(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    self_dir = proc_root / str(process_health.os.getpid())
    shell_dir = proc_root / "124"
    self_dir.mkdir(parents=True)
    shell_dir.mkdir(parents=True)
    _write_cmdline(
        self_dir / "cmdline",
        "python",
        "-m",
        "app.process_health",
        "worker",
    )
    _write_cmdline(
        shell_dir / "cmdline",
        "sh",
        "-c",
        "pgrep -f 'app.artifacts.worker worker'",
    )

    assert process_health.find_process(
        "worker",
        proc_root=proc_root,
    ) is None


def test_cli_returns_nonzero_when_process_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["process_health", "worker"])
    monkeypatch.setattr(
        process_health,
        "find_process",
        lambda *_args, **_kwargs: None,
    )

    assert process_health.main() == 1
