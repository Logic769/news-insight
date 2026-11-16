"""跨进程分析锁：同一时刻只允许一个分析批次。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.lockfile import ProcessFileLock


def test_lock_released_can_be_reacquired(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lock = ProcessFileLock()
    assert lock.acquire(blocking=False)
    lock.release()
    assert lock.acquire(blocking=False)
    lock.release()


def test_lock_blocks_another_process(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    child_code = (
        "import time\n"
        "from app.lockfile import ProcessFileLock\n"
        "lock = ProcessFileLock()\n"
        "print(lock.acquire(False), flush=True)\n"
        "time.sleep(10)\n"
    )
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    child = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "True"
        assert ProcessFileLock().acquire(blocking=False) is False
    finally:
        child.terminate()
        child.wait(timeout=10)
